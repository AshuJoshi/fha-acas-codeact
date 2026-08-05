#!/usr/bin/env python3
"""Surface solution-path variance and tail events across benchmark batches.

A median hides exactly the thing that matters most for an agentic system: did
the model solve the SAME prompt the SAME way every time, or does it sometimes
take a wildly different (and wildly more expensive) path? This script never
runs a model; it only re-reads existing ``run_*.json`` records (e.g. from two
separate batches run days apart) and answers two questions a single
mean/median cannot:

  1. SOLUTION-PATH VARIANCE -- for the same (model, prompt) seen more than
     once (across different batches/dates), did it take the same "shape" of
     solution each time? Shape = turn count, tool-type sequence, whether it
     installed a package, and which external domains it contacted. A prompt
     solved 100% of the time with the same shape is stable; one where the
     shape changes is where cognitive (planning) non-determinism lives.

  2. TAIL EVENTS -- any single run whose wall time is far beyond that
     model's own typical wall time, flagged automatically with its full
     latency decomposition (model / tool / overhead), instead of relying on
     manually noticing it (as happened with a 641s gpt-5.5 outlier).

Usage
-----
    python scripts/volatility_report.py benchmark-results/kimi-k3-* benchmark-results/aug5-*
    python scripts/volatility_report.py benchmark-results/aug5-* --tail-multiplier 4
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s'\"\\)]+")


def _load_records(paths: list[Path]) -> list[tuple[str, dict[str, Any]]]:
    """Collect (batch_label, record). batch_label is the containing -suite/-*
    directory name so repeats across separate dated runs are distinguishable."""
    out: list[tuple[str, dict[str, Any]]] = []
    files: list[Path] = []
    for p in paths:
        if p.is_file():
            files.append(p)
        else:
            files.extend(sorted(p.rglob("run_*.json")))
    for f in files:
        try:
            rec = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict) or not rec.get("prompt"):
            continue
        out.append((f.parent.name, rec))
    return out


def _domains(code: str) -> set[str]:
    out = set()
    for url in _URL_RE.findall(code):
        try:
            host = urlparse(url).netloc
        except ValueError:
            continue
        if host:
            out.add(host)
    return out


def _signature(rec: dict[str, Any]) -> tuple[Any, ...]:
    """A hashable fingerprint of the SHAPE of a run's solution path."""
    tool_calls = rec.get("tool_calls", []) or []
    tool_types = tuple("sh" if tc.get("tool") == "run_shell" else "py" for tc in tool_calls)
    installed = any(bool(tc.get("is_install")) for tc in tool_calls)
    domains: set[str] = set()
    for tc in tool_calls:
        domains |= _domains(str(tc.get("code") or tc.get("command") or ""))
    return (rec.get("num_turns", 0), tool_types, installed, tuple(sorted(domains)))


def _fmt_sig(sig: tuple[Any, ...]) -> str:
    turns, tools, installed, domains = sig
    parts = [f"turns={turns}", f"tools={','.join(tools) or '-'}"]
    if installed:
        parts.append("install=yes")
    if domains:
        parts.append(f"domains={','.join(domains)}")
    return " ".join(parts)


def _print_path_variance(records: list[tuple[str, dict[str, Any]]]) -> None:
    groups: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for batch, rec in records:
        groups[(rec.get("model", "?"), rec.get("prompt", ""))].append((batch, rec))

    print("=" * 100)
    print("SOLUTION-PATH VARIANCE (same model + prompt seen more than once)")
    print("=" * 100)
    stable, unstable = 0, 0
    unstable_rows: list[str] = []
    for (model, prompt), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        sigs = [_signature(rec) for _, rec in group]
        modal = statistics.mode(sigs)
        match_rate = sigs.count(modal) / len(sigs)
        if match_rate == 1.0:
            stable += 1
            continue
        unstable += 1
        unstable_rows.append(
            f"\n{model}  ::  {prompt[:65]}\n  match_rate={match_rate:.0%} across {len(group)} runs"
        )
        for batch, rec in group:
            wall = rec.get("total_wall_ms", 0) / 1000
            model_ms = rec.get("total_model_ms", 0) / 1000
            tool_ms = rec.get("total_tool_ms", 0) / 1000
            sig = _fmt_sig(_signature(rec))
            unstable_rows.append(
                f"    [{batch:<14}] wall={wall:6.1f}s (model={model_ms:5.1f}s tool={tool_ms:5.1f}s)  {sig}"
            )
    print(f"stable (identical path every time): {stable}")
    print(f"unstable (path changed run to run): {unstable}")
    for row in unstable_rows:
        print(row)


def _print_tail_events(records: list[tuple[str, dict[str, Any]]], multiplier: float) -> None:
    by_model: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for batch, rec in records:
        by_model[rec.get("model", "?")].append((batch, rec))

    print("\n" + "=" * 100)
    print(f"TAIL EVENTS (single run > {multiplier:.0f}x that model's own median wall time)")
    print("=" * 100)
    any_flagged = False
    for model, items in sorted(by_model.items()):
        walls = [rec.get("total_wall_ms", 0) for _, rec in items]
        med = statistics.median(walls) if walls else 0
        if med <= 0:
            continue
        for batch, rec in items:
            wall = rec.get("total_wall_ms", 0)
            if wall < med * multiplier:
                continue
            any_flagged = True
            model_ms = rec.get("total_model_ms", 0) / 1000
            tool_ms = rec.get("total_tool_ms", 0) / 1000
            overhead_ms = rec.get("agent_overhead_ms", 0) / 1000
            sig = _fmt_sig(_signature(rec))
            tag = " [infra failure, not model behavior]" if rec.get("num_turns", 0) == 0 else ""
            print(
                f"{model:<16} [{batch:<14}] wall={wall / 1000:7.1f}s "
                f"({wall / med:.1f}x its own median {med / 1000:.1f}s){tag}  "
                f"model={model_ms:.1f}s tool={tool_ms:.1f}s overhead={overhead_ms:.1f}s"
            )
            print(f"    {sig}")
            print(f"    prompt: {rec.get('prompt', '')[:80]}")
    if not any_flagged:
        print("(none)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", default=["benchmark-results"])
    ap.add_argument("--tail-multiplier", type=float, default=3.0, help="Flag runs beyond N x the model's own median wall time. Default 3.")
    args = ap.parse_args()

    records = _load_records([Path(p) for p in args.paths])
    if not records:
        print("No records found.")
        return 1
    print(f"Loaded {len(records)} records from {len(args.paths)} path argument(s).\n")

    _print_path_variance(records)
    _print_tail_events(records, args.tail_multiplier)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
