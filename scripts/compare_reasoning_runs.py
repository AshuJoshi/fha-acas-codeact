#!/usr/bin/env python3
"""Summarize repeated benchmark runs per model across one or more result folders.

Reads the run_*.json records written by compare_models.py, recursing into
subdirectories (so the batchN/ folders from run_reasoning_overnight.sh work),
and prints one row per model: effective successes, latency (mean, success-only
mean, median, P95, max), turns, tool calls, tokens, retries, and how many
prompts followed an identical solution path on every repeat.

"ok" only means a non-empty answer that did not hit a known failure marker.
It is not a factual grade; review answers separately.

Examples
--------
    uv run python scripts/compare_reasoning_runs.py benchmark-results/reasoning-repeats

    # Combine several runs and save the aggregate
    uv run python scripts/compare_reasoning_runs.py \\
        benchmark-results/reasoning-repeats \\
        benchmark-results/reasoning-repeats-mai \\
        --json benchmark-results/reasoning-comparison.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


FAILURE_MARKERS = (
    "function invocation limit reached",
    "failed to complete",
    "no final answer",
)
URL_RE = re.compile(r"https?://[^\s'\"\\)]+")


def _load_records(paths: list[Path]) -> list[dict[str, Any]]:
    files: dict[Path, None] = {}
    for path in paths:
        if path.is_file():
            files[path.resolve()] = None
        elif path.is_dir():
            for file in sorted(path.rglob("run_*.json")):
                files[file.resolve()] = None
        else:
            print(f"warning: path not found: {path}", file=sys.stderr)

    records: list[dict[str, Any]] = []
    skipped = 0
    for file in files:
        try:
            record = json.loads(file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: skipped {file}: {exc}", file=sys.stderr)
            skipped += 1
            continue
        if isinstance(record, dict) and record.get("prompt"):
            records.append(record)
        else:
            print(f"warning: skipped {file}: not a run record", file=sys.stderr)
            skipped += 1
    if skipped:
        print(f"warning: {skipped} file(s) skipped", file=sys.stderr)
    return records


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def _shape(record: dict[str, Any]) -> tuple[Any, ...]:
    calls = record.get("tool_calls") or []
    domains: set[str] = set()
    for call in calls:
        payload = str(call.get("code") or call.get("command") or "")
        for url in URL_RE.findall(payload):
            domain = urlparse(url).netloc
            if domain:
                domains.add(domain)
    return (
        record.get("num_turns", 0),
        tuple(call.get("tool", "?") for call in calls),
        bool(record.get("needed_install")),
        tuple(sorted(domains)),
    )


def _is_failure(record: dict[str, Any]) -> bool:
    answer = str(record.get("answer") or "").lower()
    return (
        not record.get("success", False)
        or record.get("num_turns", 0) == 0
        or any(marker in answer for marker in FAILURE_MARKERS)
    )


def _summarize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_model[str(record.get("model", "?"))].append(record)

    summaries: list[dict[str, Any]] = []
    for model, model_records in by_model.items():
        walls = [float(record.get("total_wall_ms", 0)) / 1000 for record in model_records]
        successful = [record for record in model_records if not _is_failure(record)]
        successful_walls = [
            float(record.get("total_wall_ms", 0)) / 1000 for record in successful
        ]
        prompts = {str(record.get("prompt")) for record in model_records}
        repeats = Counter(str(record.get("prompt")) for record in model_records)
        stable_prompts = 0
        for prompt in prompts:
            shapes = {
                _shape(record)
                for record in model_records
                if record.get("prompt") == prompt
            }
            stable_prompts += len(shapes) == 1

        summaries.append(
            {
                "model": model,
                "runs": len(model_records),
                "prompts": len(prompts),
                "min_repeats": min(repeats.values(), default=0),
                "effective_successes": len(successful),
                "failures": len(model_records) - len(successful),
                "mean_wall_s": statistics.mean(walls),
                "mean_success_wall_s": (
                    statistics.mean(successful_walls) if successful_walls else None
                ),
                "median_wall_s": statistics.median(walls),
                "p95_wall_s": _percentile(walls, 0.95),
                "max_wall_s": max(walls, default=0),
                "mean_turns": statistics.mean(
                    float(record.get("num_turns", 0)) for record in model_records
                ),
                "mean_tool_calls": statistics.mean(
                    float(record.get("num_tool_calls", 0)) for record in model_records
                ),
                "mean_total_tokens": statistics.mean(
                    float(record.get("total_tokens", 0)) for record in model_records
                ),
                "runs_with_retries": sum(
                    int(record.get("retry_count", 0) > 0) for record in model_records
                ),
                "total_retries": sum(
                    int(record.get("retry_count", 0)) for record in model_records
                ),
                "stable_prompts": stable_prompts,
            }
        )
    return sorted(summaries, key=lambda summary: summary["mean_wall_s"])


def _print_table(summaries: list[dict[str, Any]]) -> None:
    header = (
        f"{'model':<25} {'ok':>7} {'mean':>7} {'okmean':>7} {'med':>7} {'p95':>7} "
        f"{'max':>7} {'turns':>7} {'tools':>7} {'tokens':>8} {'retry':>7} {'stable':>8}"
    )
    print(header)
    print("-" * len(header))
    for item in summaries:
        ok_mean = item["mean_success_wall_s"]
        ok_mean_text = "n/a" if ok_mean is None else f"{ok_mean:.1f}"
        print(
            f"{item['model']:<25} "
            f"{item['effective_successes']:>3}/{item['runs']:<3} "
            f"{item['mean_wall_s']:>7.1f} {ok_mean_text:>7} "
            f"{item['median_wall_s']:>7.1f} "
            f"{item['p95_wall_s']:>7.1f} {item['max_wall_s']:>7.1f} "
            f"{item['mean_turns']:>7.1f} {item['mean_tool_calls']:>7.1f} "
            f"{item['mean_total_tokens']:>8.0f} "
            f"{item['runs_with_retries']:>3}/{item['total_retries']:<3} "
            f"{item['stable_prompts']:>3}/{item['prompts']:<3}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--json", type=Path, help="Write summary JSON to this path.")
    args = parser.parse_args()

    records = _load_records(args.paths)
    if not records:
        parser.error("No run records found.")
    summaries = _summarize(records)
    _print_table(summaries)
    if args.json:
        args.json.write_text(json.dumps(summaries, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())