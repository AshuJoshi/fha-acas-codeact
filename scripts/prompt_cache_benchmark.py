#!/usr/bin/env python3
"""A/B benchmark Fireworks prompt-cache routing through Microsoft Foundry.

Each model runs two isolated cohorts against the local CodeAct agent:

* ``control`` sends no cache-routing hint.
* ``affinity`` reuses one ``x-session-affinity`` value for all related calls.

Both cohorts use the same long repository-context prefix, but a cohort marker
keeps their prompt caches separate. The first request in each cohort primes the
cache and is excluded from measured averages. Cache hit rate is reported when
Foundry exposes Fireworks cache headers or cached-input token usage; model
latency is always reported as the portable comparison signal.

Usage:

    uv run --extra compare python scripts/prompt_cache_benchmark.py
    uv run --extra compare python scripts/prompt_cache_benchmark.py \
        --repeats 3 --gap-s 1 --out-dir benchmark-results/prompt-cache
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_local_codeact as local  # noqa: E402
from acas_toolkit import SandboxPool  # noqa: E402


DEFAULT_MODELS = ("kimi-k3", "minimax-m3", "deepseek-v4-pro", "glm-5.2")
PRIME_TASK = "Run Python to print the integer result of 17 * 19."
MEASURED_TASKS = (
    "Run Python to compute the sum of squares from 1 through 500 and print the integer.",
    "Run Python to print the first 30 Fibonacci numbers as a comma-separated list.",
    "Run Python to count the prime numbers below 10000 and print only the count.",
)


def _project_endpoint() -> str:
    endpoint = os.environ.get("AZURE_AI_PROJECT_ENDPOINT") or os.environ.get(
        "FOUNDRY_PROJECT_ENDPOINT"
    )
    if not endpoint:
        sys.exit(
            "AZURE_AI_PROJECT_ENDPOINT / FOUNDRY_PROJECT_ENDPOINT not set "
            "(run azd up or azd provision)."
        )
    return endpoint


def _shared_context(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as ex:
        sys.exit(f"could not read cache-prefix file {path}: {ex}")


def _prompt(*, cohort: str, context: str, task: str) -> str:
    return (
        f"Prompt-cache experiment cohort: {cohort}.\n"
        "The repository documentation below is stable context. Use it only as "
        "background, do not summarize it. Perform the task after </context>.\n\n"
        f"<context>\n{context}\n</context>\n\nTask: {task}"
    )


async def _run_one(
    *,
    pool: SandboxPool,
    model: str,
    prompt: str,
    project_endpoint: str,
    disk: str,
    session_affinity: str | None,
) -> dict[str, Any]:
    try:
        with pool.lease(disk=disk) as sandbox_id:
            record = await local._run_agent(
                pool=pool,
                sandbox_id=sandbox_id,
                model=model,
                prompt=prompt,
                project_endpoint=project_endpoint,
                api="chat",
                session_affinity=session_affinity,
            )
        record["success"] = bool((record.get("answer") or "").strip())
        record["error"] = None
        return record
    except Exception as ex:  # noqa: BLE001 - preserve the rest of the experiment
        return {
            "model": model,
            "session_affinity": session_affinity,
            "success": False,
            "error": f"{type(ex).__name__}: {ex}",
            "total_model_ms": 0.0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cached_tokens_source": "none",
            "cache_response_headers": [],
        }


def _header_int(headers: dict[str, str], name: str) -> int | None:
    value = next((v for k, v in headers.items() if k.lower() == name), None)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _cache_tokens(record: dict[str, Any]) -> tuple[int, int, str] | None:
    prompt_tokens = 0
    cached_tokens = 0
    header_observations = 0
    for headers in record.get("cache_response_headers", []) or []:
        total = _header_int(headers, "fireworks-prompt-tokens")
        cached = _header_int(headers, "fireworks-cached-prompt-tokens")
        if total is not None and cached is not None:
            prompt_tokens += total
            cached_tokens += cached
            header_observations += 1
    if header_observations:
        return prompt_tokens, cached_tokens, "headers"

    cached_tokens = int(record.get("cached_input_tokens") or 0)
    if record.get("cached_tokens_source") == "reported":
        return int(record.get("input_tokens") or 0), cached_tokens, "usage"
    return None


def _mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def _print_summary(records: list[dict[str, Any]], models: list[str]) -> None:
    print("\n" + "=" * 92)
    print("FIREWORKS PROMPT CACHE A/B (priming calls excluded)")
    print("=" * 92)
    print(
        f"{'model':<18}{'arm':<11}{'ok':>7}{'model(s)':>11}"
        f"{'prompt-tok':>13}{'cached-tok':>13}{'hit-rate':>10}"
    )
    print("-" * 92)
    for model in models:
        for cohort in ("control", "affinity"):
            selected = [
                record
                for record in records
                if record.get("model") == model
                and record.get("cohort") == cohort
                and record.get("measured")
            ]
            successful = [record for record in selected if record.get("success")]
            observations = [
                observation
                for record in successful
                if (observation := _cache_tokens(record)) is not None
            ]
            if observations:
                prompt_tokens = sum(item[0] for item in observations)
                cached_tokens = sum(item[1] for item in observations)
                hit_rate = cached_tokens / prompt_tokens if prompt_tokens else 0.0
                prompt_cell = str(prompt_tokens)
                cached_cell = str(cached_tokens)
                hit_cell = f"{hit_rate:.1%}"
            else:
                prompt_cell = cached_cell = hit_cell = "n/a"
            print(
                f"{model:<18}{cohort:<11}{len(successful):>3}/{len(selected):<3}"
                f"{_mean([r.get('total_model_ms', 0.0) for r in successful]) / 1000:>11.2f}"
                f"{prompt_cell:>13}{cached_cell:>13}{hit_cell:>10}"
            )
    print("-" * 92)
    print(
        "Cache-token columns are n/a when Foundry does not forward Fireworks "
        "cache headers or cached-input usage. Compare model(s) in that case."
    )


async def run(args: argparse.Namespace) -> int:
    models = [model.strip() for model in args.models.split(",") if model.strip()]
    context = _shared_context(Path(args.prefix_file))
    endpoint = _project_endpoint()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    run_number = 0

    with SandboxPool.from_env() as pool:
        for model in models:
            for cohort in ("control", "affinity"):
                affinity = (
                    f"{args.affinity_prefix}-{model}" if cohort == "affinity" else None
                )
                jobs = [(False, PRIME_TASK)] + [
                    (True, task)
                    for _ in range(args.repeats)
                    for task in MEASURED_TASKS
                ]
                for job_index, (measured, task) in enumerate(jobs):
                    run_number += 1
                    label = "measure" if measured else "prime"
                    print(
                        f"[cache] model={model} arm={cohort} {label} :: {task[:55]}",
                        file=sys.stderr,
                    )
                    record = await _run_one(
                        pool=pool,
                        model=model,
                        prompt=_prompt(cohort=cohort, context=context, task=task),
                        project_endpoint=endpoint,
                        disk=args.disk,
                        session_affinity=affinity,
                    )
                    record.update({"cohort": cohort, "measured": measured, "task": task})
                    records.append(record)
                    (out_dir / f"run_{run_number:03d}_{model}_{cohort}.json").write_text(
                        json.dumps(record, indent=2)
                    )
                    is_final_job = (
                        model == models[-1]
                        and cohort == "affinity"
                        and job_index == len(jobs) - 1
                    )
                    if args.gap_s > 0 and not is_final_job:
                        await asyncio.sleep(args.gap_s)

    (out_dir / "all_records.json").write_text(json.dumps(records, indent=2))
    _print_summary(records, models)
    print(f"[cache] wrote {len(records)} records to {out_dir}", file=sys.stderr)
    return 0 if all(record.get("success") for record in records) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--gap-s", type=float, default=1.0)
    parser.add_argument("--prefix-file", default="README.md")
    parser.add_argument("--affinity-prefix", default="codeact-prompt-cache")
    parser.add_argument("--disk", default=local.DEFAULT_DISK)
    parser.add_argument("--out-dir", default="benchmark-results/prompt-cache")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
