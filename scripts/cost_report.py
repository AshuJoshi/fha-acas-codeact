#!/usr/bin/env python3
"""Token, turns, and cost report for CodeAct benchmark records.

Reads ``run_*.json`` records and aggregates, per model, the numbers that drive
cost: average turns, input tokens, output tokens, total tokens, plus the
throughput and wall decomposition. If a price table is provided (see PRICES),
it also computes cost per task.

Why input tokens matter: the agent loop re-sends the growing conversation on
every turn, so input usually dominates total tokens and is NOT constant across
models (a verbose model, or one that takes more turns, feeds itself more input).

Purely offline: no Azure calls, no model calls.

Usage
-----
    python scripts/cost_report.py benchmark-results/v2-easy-suite
    python scripts/cost_report.py benchmark-results/v2-* --prices prices.json

``prices.json`` (or the PRICES dict below) maps a model to USD per 1M tokens:
    {"gpt-5.4": {"input": 1.25, "output": 10.0}, ...}
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Optional

# Per-1M-token prices in USD. Leave empty until confirmed; models without an
# entry show token counts only (cost columns print "n/a"). Fill from the Azure
# model pricing page (gpt-5.4) and Fireworks / Azure partner pricing.
PRICES: dict[str, dict[str, float]] = {
    # "gpt-5.4": {"input": 0.0, "output": 0.0},
    # "gpt-oss-120b": {"input": 0.0, "output": 0.0},
    # "glm-5.2": {"input": 0.0, "output": 0.0},
    # "kimi-k2.7-code": {"input": 0.0, "output": 0.0},
    # "deepseek-v4-pro": {"input": 0.0, "output": 0.0},
    # "minimax-m2.5": {"input": 0.0, "output": 0.0},
}


def _load_records(paths: list[Path]) -> list[dict[str, Any]]:
    files: list[Path] = []
    for p in paths:
        if p.is_file():
            files.append(p)
        else:
            files.extend(sorted(p.rglob("run_*.json")))
    out: list[dict[str, Any]] = []
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        recs = data if isinstance(data, list) else [data]
        for rec in recs:
            if isinstance(rec, dict) and rec.get("prompt"):
                out.append(rec)
    return out


def _avg(xs: list[float]) -> float:
    return statistics.mean(xs) if xs else 0.0


def _cost_per_task(model: str, in_tok: float, out_tok: float) -> Optional[float]:
    price = PRICES.get(model)
    if not price:
        return None
    return in_tok / 1e6 * price.get("input", 0.0) + out_tok / 1e6 * price.get(
        "output", 0.0
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("paths", nargs="*", default=["benchmark-results"])
    ap.add_argument("--prices", help="JSON file mapping model -> {input, output} USD per 1M tokens.")
    args = ap.parse_args()

    if args.prices:
        PRICES.update(json.loads(Path(args.prices).read_text()))

    records = _load_records([Path(p) for p in args.paths])
    if not records:
        print("No records found.")
        return 1

    # Only count runs that actually reported token usage (chat API reports for
    # all models; the Responses path returns zero for the Fireworks models).
    models = sorted({r.get("model", "?") for r in records})
    priced = any(m in PRICES for m in models)

    print(f"Loaded {len(records)} records from {', '.join(args.paths)}.\n")
    print("=" * 108)
    print("TOKENS / TURNS / COST  (averages per run; token-reporting runs only)")
    print("=" * 108)
    hdr = (
        f"{'model':<18}{'runs':>6}{'ok':>5}{'turns':>7}{'in-tok':>9}{'out-tok':>9}"
        f"{'tot-tok':>9}{'tok/s':>8}{'wall(s)':>9}{'$/task':>10}"
    )
    print(hdr)
    print("-" * 108)
    for m in models:
        rs = [r for r in records if r.get("model") == m]
        ok = [r for r in rs if (r.get("answer") or "").strip()]
        rep = [r for r in ok if r.get("tokens_source") == "reported"]
        turns = _avg([r.get("num_turns", 0) for r in ok])
        in_tok = _avg([r.get("input_tokens", 0) for r in rep])
        out_tok = _avg([r.get("output_tokens", 0) for r in rep])
        tot_tok = _avg([r.get("total_tokens", 0) for r in rep])
        tps = _avg([r["output_tokens_per_s"] for r in rep if r.get("output_tokens_per_s")])
        wall = _avg([r.get("total_wall_ms", 0) for r in ok]) / 1000.0
        cost = _cost_per_task(m, in_tok, out_tok)
        cost_cell = f"${cost:.5f}" if cost is not None else ("n/a" if not rep else "no price")
        tok_cell = lambda v: f"{v:.0f}" if rep else "n/a"  # noqa: E731
        print(
            f"{m:<18}{len(rs):>6}{len(ok):>4}/{len(rs):<1}"
            f"{turns:>6.1f}{tok_cell(in_tok):>9}{tok_cell(out_tok):>9}{tok_cell(tot_tok):>9}"
            f"{(f'{tps:.1f}' if rep else 'n/a'):>8}{wall:>9.2f}{cost_cell:>10}"
        )
    print("-" * 108)
    print(
        "in-tok grows with turns + a model's own verbosity (loop re-sends the "
        "conversation each turn); tot-tok = in + out."
    )
    if not priced:
        print(
            "\nNo prices set -> $/task shows 'no price'. Add per-1M-token rates to "
            "PRICES in this file or pass --prices prices.json."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
