#!/usr/bin/env python3
"""Multi-model A/B benchmark for the local CodeAct agent.

Runs a suite of coding prompts through several models (each on its own fresh ACA
Sandbox), reusing the local agent in ``run_local_codeact.py`` so every model sees
the identical agent (same instructions + tools). Captures per-run metrics — turns,
per-model-call latency, the generated code + ExecResults, latency decomposition —
and prints a side-by-side comparison. Writes all raw records to JSON for offline
coding-quality evaluation.

Fair-comparison choices:
  * one FRESH sandbox per run (clean state; installs don't leak across runs);
  * paced (``--gap-s``) to avoid hammering a model's rate limit;
  * optional ``--repeats`` with averaging.

Env: auto-loaded from the active azd env (see run_local_codeact). Auth: az login.

Examples
--------
    # Default suite, all models in DEFAULT_MODELS, one repeat, results to ./benchmark-results
    uv run --extra compare python scripts/compare_models.py

    # Three repeats, 5s pacing, custom models, custom prompts file (one per line)
    uv run --extra compare python scripts/compare_models.py \\
        --models gpt-5.4,glm-5.2,kimi-k2.7-code --repeats 3 --gap-s 5 \\
        --prompts prompts.txt --out-dir benchmark-results
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# Import the local agent (its module-level env load + AF imports run on import).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_local_codeact as local  # noqa: E402
from acas_toolkit import SandboxPool  # noqa: E402


DEFAULT_MODELS = ["gpt-5.4", "glm-5.2", "kimi-k2.7-code"]
# A trivial prompt used only to warm a model's endpoint before its measured
# suite (the first inference on a freshly-deployed/idle model can be far slower
# than steady state — a provider-side model cold-start, not the sandbox).
WARMUP_PROMPT = "Reply with the single word: ready."
DEFAULT_PROMPTS = [
    "Compute the sum of squares from 1 to 100 and print only the integer.",
    "Compute the first 20 Fibonacci numbers and print them as a comma-separated list.",
    "Write and run a function that returns all prime numbers below 50, then print the list.",
    "Parse the CSV text 'a,b\\n1,2\\n3,4\\n5,6' with the csv module and print the sum of column b.",
    "Reverse the string 'benchmarking' without using slicing, and print the result.",
]


async def _bench_one(
    *, pool: SandboxPool, model: str, prompt: str, disk: str, project_endpoint: str, api: str
) -> dict[str, Any]:
    """Run one (model, prompt) on a fresh sandbox; never raise — capture failures."""
    t0 = time.monotonic()
    try:
        with pool.lease(disk=disk) as sbx_id:
            rec = await local._run_agent(
                pool=pool,
                sandbox_id=sbx_id,
                model=model,
                prompt=prompt,
                project_endpoint=project_endpoint,
                api=api,
            )
        rec["success"] = bool((rec.get("answer") or "").strip())
        rec["error"] = None
    except Exception as ex:  # noqa: BLE001 - benchmark must survive any single failure
        rec = {
            "model": model,
            "api": api,
            "prompt": prompt,
            "success": False,
            "error": f"{type(ex).__name__}: {ex}",
            "total_wall_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "wall_excl_install_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "num_turns": 0,
            "model_call_ms": [],
            "total_model_ms": 0.0,
            "tokens_source": "none",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "reasoning_tokens": 0,
            "ms_per_output_token": None,
            "output_tokens_per_s": None,
            "model_call_usage": [],
            "num_tool_calls": 0,
            "total_tool_ms": 0.0,
            "needed_install": False,
            "install_ms": 0.0,
            "exec_ms": 0.0,
            "agent_overhead_ms": 0.0,
            "tool_calls": [],
            "answer": "",
        }
    return rec


def _avg(xs: list[float]) -> float:
    return round(statistics.mean(xs), 1) if xs else 0.0


def _summarize(records: list[dict[str, Any]], models: list[str], cold_start: dict[str, float] | None = None) -> None:
    print("\n" + "=" * 104)
    print("MODEL COMPARISON")
    print("=" * 104)
    hdr = (
        f"{'model':<16}{'runs':>5}{'ok':>5}{'wall(s)':>9}{'turns':>7}"
        f"{'model(s)':>10}{'out-tok':>9}{'tok/s':>8}{'tool(s)':>9}{'install':>9}"
    )
    print(hdr)
    print("-" * 104)
    for m in models:
        rs = [r for r in records if r["model"] == m]
        ok = [r for r in rs if r.get("success")]
        n_install = sum(1 for r in ok if r.get("needed_install"))
        reported = [r for r in ok if r.get("tokens_source") == "reported"]
        tps = [r["output_tokens_per_s"] for r in reported if r.get("output_tokens_per_s")]
        out_tok = _avg([r.get("output_tokens", 0) for r in reported]) if reported else 0
        out_tok_cell = f"{out_tok:.0f}" if reported else "n/a"
        tps_cell = f"{_avg(tps):.1f}" if tps else "n/a"
        print(
            f"{m:<16}{len(rs):>5}{len(ok):>4}/{len(rs):<1}"
            f"{_avg([r['total_wall_ms'] for r in ok]) / 1000:>9.2f}"
            f"{_avg([r['num_turns'] for r in ok]):>7.1f}"
            f"{_avg([r['total_model_ms'] for r in ok]) / 1000:>10.2f}"
            f"{out_tok_cell:>9}"
            f"{tps_cell:>8}"
            f"{_avg([r['total_tool_ms'] for r in ok]) / 1000:>9.2f}"
            f"{n_install:>6}/{len(ok):<2}"
        )
    print("-" * 104)
    if cold_start:
        print("Cold-start (first/warm-up call per model, EXCLUDED from the averages above):")
        for m in models:
            if m in cold_start:
                print(f"  - {m:<16} {cold_start[m] / 1000:.1f}s")
        print("-" * 104)
    # Per-prompt, model-vs-model (first repeat only, for a readable snapshot).
    prompts = []
    for r in records:
        if r["prompt"] not in prompts:
            prompts.append(r["prompt"])
    print("Per-prompt wall time (s) — first run of each:")
    for p in prompts:
        cells = []
        for m in models:
            r = next((x for x in records if x["model"] == m and x["prompt"] == p), None)
            if r is None:
                cells.append(f"{m}=--")
            else:
                mark = "" if r.get("success") else "✗"
                cells.append(f"{m}={r['total_wall_ms'] / 1000:.1f}{mark}")
        print(f"  - {p[:60]:<60} {'  '.join(cells)}")
    print("=" * 104)


async def run(args: argparse.Namespace) -> int:
    project_endpoint = (
        os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
        or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    )
    if not project_endpoint:
        sys.exit("AZURE_AI_PROJECT_ENDPOINT / FOUNDRY_PROJECT_ENDPOINT not set (run azd up).")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.prompts:
        prompts = [ln.strip() for ln in Path(args.prompts).read_text().splitlines() if ln.strip()]
    else:
        prompts = DEFAULT_PROMPTS

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    total = len(models) * len(prompts) * args.repeats
    print(
        f"[bench] {len(models)} models x {len(prompts)} prompts x {args.repeats} repeats "
        f"= {total} runs; gap={args.gap_s}s; out={out_dir}",
        file=sys.stderr,
    )

    records: list[dict[str, Any]] = []
    cold_start: dict[str, float] = {}
    n = 0
    with SandboxPool.from_env() as pool:
        for model in models:
            if args.warmup:
                print(f"[bench] warming up {model} (excluded from averages)", file=sys.stderr)
                warm = await _bench_one(
                    pool=pool,
                    model=model,
                    prompt=WARMUP_PROMPT,
                    disk=args.disk,
                    project_endpoint=project_endpoint,
                    api=args.api,
                )
                cold_start[model] = warm.get("total_wall_ms", 0.0)
                print(
                    f"[bench] {model} cold-start (first call): "
                    f"{cold_start[model] / 1000:.1f}s",
                    file=sys.stderr,
                )
                if args.gap_s > 0:
                    await asyncio.sleep(args.gap_s)
            for rep in range(args.repeats):
                for prompt in prompts:
                    n += 1
                    print(
                        f"[bench] ({n}/{total}) model={model} rep={rep + 1} :: {prompt[:56]}",
                        file=sys.stderr,
                    )
                    rec = await _bench_one(
                        pool=pool,
                        model=model,
                        prompt=prompt,
                        disk=args.disk,
                        project_endpoint=project_endpoint,
                        api=args.api,
                    )
                    rec["repeat"] = rep + 1
                    records.append(rec)
                    (out_dir / f"run_{n:03d}_{model}_{rep + 1}.json").write_text(
                        json.dumps(rec, indent=2)
                    )
                    if n < total and args.gap_s > 0:
                        await asyncio.sleep(args.gap_s)

    (out_dir / "all_records.json").write_text(json.dumps(records, indent=2))
    _summarize(records, models, cold_start)
    print(f"[bench] wrote {len(records)} records to {out_dir}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--models", default=",".join(DEFAULT_MODELS), help="Comma-separated model deployment names.")
    p.add_argument("--prompts", help="Path to a prompts file (one prompt per line). Default: built-in suite.")
    p.add_argument("--repeats", type=int, default=1, help="Repeats per (model, prompt) for averaging. Default: 1.")
    p.add_argument("--gap-s", type=float, default=3.0, help="Seconds to pause between runs. Default: 3.")
    p.add_argument(
        "--warmup",
        action="store_true",
        help="Send one throwaway call per model first to warm its endpoint; "
        "excluded from the averages and reported separately as cold-start.",
    )
    p.add_argument("--disk", default=local.DEFAULT_DISK, help=f"Sandbox disk image (default: {local.DEFAULT_DISK}).")
    p.add_argument(
        "--api",
        choices=["chat", "responses"],
        default=local.DEFAULT_API,
        help="Model call surface. 'chat' (Chat Completions) reports token usage "
        "for all models incl. Fireworks; 'responses' matches the FHA. "
        f"Default: {local.DEFAULT_API}.",
    )
    p.add_argument("--out-dir", default="benchmark-results", help="Directory for per-run + aggregate JSON (gitignored). Default: benchmark-results.")
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
