#!/usr/bin/env python3
"""Method-selection experiment: does an execution-capable agent choose to execute?

Runs the same model, prompts, API, reasoning effort and tools under two instruction
arms and records which path the agent took (direct answer, code, retrieval through
code). New files only; the existing CodeAct scripts, prompts and agent are imported
and left unchanged.

  Arm A: the current CodeAct INSTRUCTIONS, verbatim.
  Arm B: the same text with the opening tool directives and the computation-preference
         bullet replaced by instructions-neutral.txt.

Each arm runs in its own process (the runner reads a module-level INSTRUCTIONS value),
every run gets a fresh ACA Sandbox, and arms alternate per repeat so time of day does
not favor one arm.

    uv run --extra compare python experiments/method-selection/run_experiment.py run --repeats 1
    uv run --extra compare python experiments/method-selection/run_experiment.py summarize RESULTS_DIR
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

ARMS = ("A", "B")

# Text of the current instructions that arm B replaces. build_instructions() fails if
# the source text changes, so a silent drift cannot leak into the comparison.
OPENING = (
    "Use run_shell for environment setup, package installation, and file inspection.\n"
    "Use execute_code for Python 3 code execution.\n"
)
PREFERENCE_BULLET = (
    "- When the user asks for computation, data transformation, debugging, or code\n"
    "  execution, prefer using the tools instead of answering from memory.\n"
)

_NET_RE = re.compile(r"urllib|requests|httpx|http\.client|https?://|\bcurl\b|\bwget\b|socket", re.I)


def build_instructions(arm: str, base: str, neutral: str) -> str:
    if arm == "A":
        return base
    if arm != "B":
        raise ValueError(f"unknown arm {arm!r}")
    for part in (OPENING, PREFERENCE_BULLET):
        if base.count(part) != 1:
            raise RuntimeError(
                "CodeAct INSTRUCTIONS changed; update OPENING/PREFERENCE_BULLET before running arm B."
            )
    return base.replace(OPENING, neutral.strip() + "\n").replace(PREFERENCE_BULLET, "")


def classify(tool_calls: list[dict[str, Any]]) -> str:
    """direct | execution | retrieval_via_code | mixed (heuristic on the recorded code)."""
    if not tool_calls:
        return "direct"
    net = [bool(_NET_RE.search(t.get("code") or t.get("command") or "")) for t in tool_calls]
    if all(net):
        return "retrieval_via_code"
    return "mixed" if any(net) else "execution"


def check_answer(answer: str, pattern: str | None) -> bool | None:
    if pattern is None:
        return None
    return re.search(pattern, (answer or "").replace(",", "")) is not None


def load_config() -> dict[str, Any]:
    cfg = json.loads((HERE / "experiment-config.json").read_text())
    prompts = load_prompts()
    unknown = set(cfg.get("expectations", {})) - set(prompts)
    if unknown:
        sys.exit(f"experiment-config.json expectations match no prompt: {sorted(unknown)}")
    return cfg


def load_prompts() -> list[str]:
    return [ln.strip() for ln in (HERE / "prompts.txt").read_text().splitlines() if ln.strip()]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def _worker(args: argparse.Namespace) -> int:
    import compare_models  # noqa: PLC0415 - heavy imports only in the worker process
    import run_local_codeact as local  # noqa: PLC0415
    from acas_toolkit import SandboxPool  # noqa: PLC0415

    cfg = load_config()
    instructions = build_instructions(args.arm, local.INSTRUCTIONS, (HERE / "instructions-neutral.txt").read_text())
    local.INSTRUCTIONS = instructions
    project_endpoint = os.environ.get("AZURE_AI_PROJECT_ENDPOINT") or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not project_endpoint:
        sys.exit("AZURE_AI_PROJECT_ENDPOINT / FOUNDRY_PROJECT_ENDPOINT not set.")

    prompts = load_prompts()
    out = Path(args.out) / args.arm
    out.mkdir(parents=True, exist_ok=True)
    with SandboxPool.from_env() as pool:
        for idx, prompt in enumerate(prompts, 1):
            print(f"[exp] arm={args.arm} rep={args.rep} ({idx}/{len(prompts)}) :: {prompt[:60]}", file=sys.stderr)
            rec = await compare_models._bench_one(
                pool=pool,
                model=cfg["model"],
                prompt=prompt,
                disk=local.DEFAULT_DISK,
                project_endpoint=project_endpoint,
                api=cfg["api"],
                reasoning_effort=cfg["reasoning_effort"],
            )
            rec.update(
                arm=args.arm,
                repeat=args.rep,
                prompt_index=idx,
                instructions_sha256=_sha(instructions),
                path=classify(rec.get("tool_calls") or []) if rec["success"] else "blocked",
                correct=check_answer(rec.get("answer", ""), cfg.get("expectations", {}).get(prompt)),
            )
            (out / f"rep{args.rep}_p{idx:02d}.json").write_text(json.dumps(rec, indent=2))
            await asyncio.sleep(cfg["gap_s"])
    return 0


def _deployment_info(model: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "az", "cognitiveservices", "account", "deployment", "show",
            "-n", os.environ.get("FOUNDRY_ACCOUNT_NAME", ""), "-g", os.environ.get("AZURE_RESOURCE_GROUP", ""),
            "--deployment-name", model, "-o", "json",
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()[:300]}
    d = json.loads(proc.stdout)
    return {
        "name": d["name"], "model": d["properties"]["model"], "sku": d["sku"],
        "state": d["properties"]["provisioningState"],
    }


def _driver(args: argparse.Namespace) -> int:
    import run_local_codeact as local  # noqa: PLC0415 - also loads the azd env
    import write_run_meta  # noqa: PLC0415

    cfg = load_config()
    repeats = args.repeats or cfg["repeats"]
    label = args.label or f"{datetime.now(timezone.utc):%Y-%m-%d}-{cfg['model']}"
    out = (HERE / "results" / label).resolve()
    if out.exists() and any(out.rglob("rep*_p*.json")):
        sys.exit(f"{out} already has results; pass a new --label")
    out.mkdir(parents=True, exist_ok=True)

    neutral = (HERE / "instructions-neutral.txt").read_text()
    for arm in ARMS:
        (out / f"instructions-{arm}.txt").write_text(build_instructions(arm, local.INSTRUCTIONS, neutral))
    (out / "prompts.txt").write_text((HERE / "prompts.txt").read_text())
    (out / "experiment-config.json").write_text((HERE / "experiment-config.json").read_text())
    deployment = _deployment_info(cfg["model"])
    (out / "deployment.json").write_text(json.dumps(deployment, indent=2))
    print(f"[exp] deployment: {json.dumps(deployment)}", file=sys.stderr)

    os.environ.update(
        MODELS=cfg["model"], API=cfg["api"], EFFORT=cfg["reasoning_effort"], GAP_S=str(cfg["gap_s"]),
        REPEATS=str(repeats), BATCH_SIZE="1", PARALLEL="", PROMPTS="experiments/method-selection/prompts.txt",
    )
    write_run_meta.main(["start", str(out), "--", "--experiment", "method-selection"])

    failed = 0
    for rep in range(1, repeats + 1):
        for arm in ARMS:
            rc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "worker", "--arm", arm, "--rep", str(rep), "--out", str(out)]
            ).returncode
            failed += rc != 0
    write_run_meta.main(["finish", str(out), "ok" if not failed else "failed"])
    _summarize(out)
    return 1 if failed else 0


def _summarize(out: Path) -> None:
    recs = [json.loads(f.read_text()) for f in sorted(out.rglob("rep*_p*.json"))]
    cells: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in recs:
        cells[(r["prompt_index"], r["prompt"], r["arm"])].append(r)
    rows = []
    print(f"\n{'prompt':<58} arm  n  direct exec retr mixed blocked  correct  reason_tok  wall_s")
    for (idx, prompt, arm), rs in sorted(cells.items()):
        paths = [r["path"] for r in rs]
        graded = [r["correct"] for r in rs if r["correct"] is not None]
        reasoning = [r.get("reasoning_tokens") or 0 for r in rs]
        row = {
            "prompt": prompt, "arm": arm, "n": len(rs),
            **{p: paths.count(p) for p in ("direct", "execution", "retrieval_via_code", "mixed", "blocked")},
            "correct": f"{sum(graded)}/{len(graded)}" if graded else "n/a",
            "mean_reasoning_tokens": round(sum(reasoning) / len(rs)),
            "mean_wall_s": round(sum(r["total_wall_ms"] for r in rs) / len(rs) / 1000, 1),
        }
        rows.append(row)
        print(
            f"{prompt[:57]:<58} {arm:<3}{row['n']:>3}{row['direct']:>7}{row['execution']:>5}"
            f"{row['retrieval_via_code']:>5}{row['mixed']:>6}{row['blocked']:>8}{row['correct']:>9}"
            f"{row['mean_reasoning_tokens']:>12}{row['mean_wall_s']:>8}"
        )
    (out / "summary.json").write_text(json.dumps(rows, indent=2))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="Run both arms (alternating per repeat).")
    run.add_argument("--repeats", type=int, help="Override repeats from experiment-config.json.")
    run.add_argument("--label", help="Results folder name under results/. Default: DATE-MODEL.")
    worker = sub.add_parser("worker")
    worker.add_argument("--arm", choices=ARMS, required=True)
    worker.add_argument("--rep", type=int, required=True)
    worker.add_argument("--out", required=True)
    summ = sub.add_parser("summarize", help="Print the per-prompt, per-arm table for a results folder.")
    summ.add_argument("results_dir", type=Path)
    args = p.parse_args()
    if args.cmd == "run":
        return _driver(args)
    if args.cmd == "worker":
        return asyncio.run(_worker(args))
    _summarize(args.results_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
