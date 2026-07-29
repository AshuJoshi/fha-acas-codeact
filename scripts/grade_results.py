#!/usr/bin/env python3
"""Grade existing CodeAct benchmark records: correctness + behavior tags.

Reads the ``run_*.json`` records written by ``compare_models.py`` /
``run_local_codeact.py`` and produces two things the raw harness does not:

  1. A deterministic PASS/FAIL table for the prompts whose answers are
     checkable (easy suite in full, plus several hard-suite prompts). The
     harness only records ``success = non-empty answer``; this checks the
     answer is actually correct.
  2. A BEHAVIOR scorecard across the vague/hard/outcome suites: did the model
     decide to use the tool, did it hit a missing dependency and recover
     (install vs stdlib fallback), did the environment force extra turns, how
     verbose was the answer. These are the qualitative findings captured in
     earlier testing, counted per model.

Purely offline: no Azure calls, no model calls. It only reads files on disk.

Usage
-----
    # Grade everything under benchmark-results (recurses into *-suite dirs)
    python scripts/grade_results.py

    # Grade specific suites and emit machine-readable JSON
    python scripts/grade_results.py benchmark-results/hard-suite --json graded.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Deterministic graders: (substring that identifies the prompt) -> checker.
# A checker returns (True|False, detail). Prompts with no checker are graded
# as None (behavior-only) and excluded from the accuracy numbers.
# ---------------------------------------------------------------------------

PRIMES_BELOW_50 = {2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47}

Checker = Callable[[str, dict[str, Any]], tuple[bool, str]]


def _ints(blob: str) -> set[int]:
    return {int(x) for x in re.findall(r"-?\d+", blob)}


def _chk_sum_squares(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    return ("338350" in blob, "expected 338350")


def _chk_fib20(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    found = _ints(blob)
    # 20th term is 4181 (starting 0,1) or 6765 (starting 1,1); accept either.
    ok = 4181 in found or 6765 in found
    return (ok, "expected the 20-term Fibonacci list (…4181 or …6765)")


def _chk_primes_50(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    found = _ints(blob)
    ok = PRIMES_BELOW_50.issubset(found)
    return (ok, "expected all primes below 50 (2..47, 15 values)")


def _chk_csv_colb(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    return (bool(re.search(r"\b12\b", blob)), "expected column-b sum = 12")


def _chk_reverse(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    return ("gnikramhcneb" in blob.lower(), "expected 'gnikramhcneb'")


def _chk_humanize(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    return ("42nd" in blob.lower(), "expected ordinal '42nd'")


def _chk_median(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    return ("2.5" in blob, "expected corrected median of [1,2,3,4] = 2.5")


def _chk_quicksort(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    return ("all ok" in blob.lower(), "expected 'ALL OK'")


def _chk_notes_lines(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    # The only relevant integer for this prompt is the line count (3). Accept a
    # standalone 3 anywhere in stdout/answer; do NOT require the word "line",
    # since a terse model may just answer "3".
    ok = bool(re.search(r"\b3\b", blob))
    return (ok, "expected a line count of 3")


def _chk_fib35(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    return ("9227465" in blob, "expected 35th Fibonacci = 9227465")


def _chk_clean_names(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    wanted = ["Alice", "Bob", "Charlie", "Dave"]
    ok = all(w in blob for w in wanted)
    return (ok, "expected cleaned list Alice, Bob, Charlie, Dave")


def _chk_day_of_week(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    return ("saturday" in blob.lower(), "expected 'Saturday' (1 Jan 2000)")


def _chk_primes_below_1m(blob: str, rec: dict[str, Any]) -> tuple[bool, str]:
    return ("78498" in blob, "expected 78498 primes below one million")


# Ordered so more specific substrings win; matched against the prompt text.
DETERMINISTIC: list[tuple[str, Checker]] = [
    ("sum of squares", _chk_sum_squares),
    ("first 20 fibonacci", _chk_fib20),
    ("prime numbers below 50", _chk_primes_50),
    ("sum of column b", _chk_csv_colb),
    ("reverse the string 'benchmarking'", _chk_reverse),
    ("humanize", _chk_humanize),
    ("buggy function: def median", _chk_median),
    ("implement quicksort", _chk_quicksort),
    ("35th fibonacci", _chk_fib35),
    ("messy name list", _chk_clean_names),
    ("notes.txt", _chk_notes_lines),
    ("what day of the week", _chk_day_of_week),
    ("prime numbers are there below one million", _chk_primes_below_1m),
]


def _match_checker(prompt: str) -> Optional[Checker]:
    p = prompt.lower()
    for needle, fn in DETERMINISTIC:
        if needle in p:
            return fn
    return None


# ---------------------------------------------------------------------------
# Suite inference (from the directory name, else from the prompt).
# ---------------------------------------------------------------------------

def _suite_of(path: Path, prompt: str) -> str:
    for part in path.parts:
        if part.endswith("-suite"):
            return part[: -len("-suite")]
    return "unknown"


# ---------------------------------------------------------------------------
# Behavior extraction from a single record.
# ---------------------------------------------------------------------------

def _tool_results(rec: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for tc in rec.get("tool_calls", []) or []:
        r = tc.get("result")
        if isinstance(r, dict):
            out.append(r)
    return out


def _exit_ok(r: dict[str, Any]) -> bool:
    ec = r.get("exit_code")
    if ec is not None:
        return ec == 0
    return str(r.get("status", "")).lower() in {"ok", "success", "completed"}


def _observed_blob(rec: dict[str, Any]) -> str:
    """stdout of every tool call plus the final answer: what the model 'showed'."""
    parts: list[str] = []
    for tc in rec.get("tool_calls", []) or []:
        r = tc.get("result")
        if isinstance(r, dict) and r.get("stdout"):
            parts.append(str(r["stdout"]))
    parts.append(str(rec.get("answer") or ""))
    return "\n".join(parts)


def _behaviors(rec: dict[str, Any]) -> dict[str, Any]:
    tool_calls = rec.get("tool_calls", []) or []
    results = _tool_results(rec)
    stderrs = " ".join(str(r.get("stderr") or "") for r in results)
    codes = " ".join(
        str(tc.get("code") or tc.get("command") or "") for tc in tool_calls
    ).lower()

    module_not_found = "modulenotfounderror" in stderrs.lower()
    any_error = any(not _exit_ok(r) for r in results)
    any_ok_after_error = False
    if any_error:
        seen_error = False
        for r in results:
            if not _exit_ok(r):
                seen_error = True
            elif seen_error and _exit_ok(r):
                any_ok_after_error = True
                break

    answer = str(rec.get("answer") or "")
    return {
        "used_tool": len(tool_calls) > 0,
        "num_turns": rec.get("num_turns", 0) or 0,
        "extra_turns": (rec.get("num_turns", 0) or 0) > 2,
        "installed": bool(rec.get("needed_install")),
        "module_not_found": module_not_found,
        "recovered": bool(any_error and answer.strip() and any_ok_after_error),
        "stdlib_fallback": bool(
            module_not_found and ("urllib" in codes or "http.client" in codes)
        ),
        "table_in_answer": ("|" in answer and "---" in answer),
        "answer_chars": len(answer),
    }


# ---------------------------------------------------------------------------
# Loading records.
# ---------------------------------------------------------------------------

def _load_records(paths: list[Path]) -> list[tuple[Path, dict[str, Any]]]:
    """Collect (source_dir, record). Prefer run_*.json; ignore all_records.json
    to avoid double-counting the same runs."""
    out: list[tuple[Path, dict[str, Any]]] = []
    files: list[Path] = []
    for p in paths:
        if p.is_file():
            files.append(p)
        else:
            files.extend(sorted(p.rglob("run_*.json")))
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        recs = data if isinstance(data, list) else [data]
        for rec in recs:
            if isinstance(rec, dict) and rec.get("prompt"):
                out.append((f.parent, rec))
    return out


# ---------------------------------------------------------------------------
# Grading + aggregation.
# ---------------------------------------------------------------------------

def grade(records: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    graded: list[dict[str, Any]] = []
    for src, rec in records:
        prompt = rec["prompt"]
        checker = _match_checker(prompt)
        blob = _observed_blob(rec)
        if checker is not None:
            passed, detail = checker(blob, rec)
            correct: Optional[bool] = bool(passed)
        else:
            correct, detail = None, "behavior-only (non-deterministic)"
        graded.append(
            {
                "model": rec.get("model", "?"),
                "api": rec.get("api", "?"),
                "suite": _suite_of(src, prompt),
                "prompt": prompt,
                "correct": correct,
                "detail": detail,
                "behaviors": _behaviors(rec),
            }
        )
    return {"graded": graded}


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:.0f}%" if d else "n/a"


def _print_accuracy(graded: list[dict[str, Any]]) -> None:
    models = sorted({g["model"] for g in graded})
    print("\n" + "=" * 78)
    print("CORRECTNESS (deterministic prompts only; behavior-only prompts excluded)")
    print("=" * 78)
    print(f"{'model':<18}{'graded':>8}{'passed':>8}{'accuracy':>10}")
    print("-" * 78)
    for m in models:
        gs = [g for g in graded if g["model"] == m and g["correct"] is not None]
        passed = sum(1 for g in gs if g["correct"])
        print(f"{m:<18}{len(gs):>8}{passed:>8}{_pct(passed, len(gs)):>10}")
    print("-" * 78)


def _print_per_prompt(graded: list[dict[str, Any]]) -> None:
    models = sorted({g["model"] for g in graded})
    prompts = []
    for g in graded:
        if g["correct"] is not None and g["prompt"] not in prompts:
            prompts.append(g["prompt"])
    if not prompts:
        return
    print("\nPer-prompt correctness (deterministic prompts):")
    for p in prompts:
        cells = []
        for m in models:
            gs = [g for g in graded if g["model"] == m and g["prompt"] == p]
            if not gs:
                cells.append(f"{m}=--")
            else:
                n_ok = sum(1 for g in gs if g["correct"])
                cells.append(f"{m}={n_ok}/{len(gs)}")
        print(f"  - {p[:52]:<52} {'  '.join(cells)}")


def _print_behavior(graded: list[dict[str, Any]]) -> None:
    models = sorted({g["model"] for g in graded})
    print("\n" + "=" * 96)
    print("BEHAVIOR SCORECARD (all suites)")
    print("=" * 96)
    hdr = (
        f"{'model':<18}{'runs':>6}{'used-tool':>10}{'installed':>10}"
        f"{'MNFE':>6}{'recovered':>11}{'stdlib-fb':>10}{'>2 turns':>9}"
        f"{'tables':>8}{'ans-chars':>10}"
    )
    print(hdr)
    print("-" * 96)
    for m in models:
        gs = [g for g in graded if g["model"] == m]
        b = [g["behaviors"] for g in gs]
        n = len(b)

        def c(key: str) -> int:
            return sum(1 for x in b if x.get(key))

        chars = _avg([x["answer_chars"] for x in b])
        print(
            f"{m:<18}{n:>6}{c('used_tool'):>10}{c('installed'):>10}"
            f"{c('module_not_found'):>6}{c('recovered'):>11}{c('stdlib_fallback'):>10}"
            f"{c('extra_turns'):>9}{c('table_in_answer'):>8}{chars:>10.0f}"
        )
    print("-" * 96)
    print(
        "used-tool: reached for execute_code/run_shell; MNFE: hit ModuleNotFoundError; "
        "recovered: errored then finished; stdlib-fb: fell back to urllib/http.client;"
    )
    print(
        "tables: markdown table in answer (verbosity signal); ans-chars: avg answer length."
    )


def _avg(xs: list[float]) -> float:
    return statistics.mean(xs) if xs else 0.0


def _print_by_suite(graded: list[dict[str, Any]]) -> None:
    suites = sorted({g["suite"] for g in graded})
    print("\n" + "=" * 78)
    print("BY SUITE")
    print("=" * 78)
    for s in suites:
        gs = [g for g in graded if g["suite"] == s]
        det = [g for g in gs if g["correct"] is not None]
        passed = sum(1 for g in det if g["correct"])
        used = sum(1 for g in gs if g["behaviors"]["used_tool"])
        rec = sum(1 for g in gs if g["behaviors"]["recovered"])
        print(
            f"  {s:<12} runs={len(gs):<4} graded={len(det):<3} "
            f"passed={passed:<3} ({_pct(passed, len(det))})  "
            f"used-tool={used}/{len(gs)}  recovered={rec}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "paths",
        nargs="*",
        default=["benchmark-results"],
        help="Files or directories of run_*.json records. Default: benchmark-results.",
    )
    ap.add_argument("--json", help="Write the full graded records to this JSON file.")
    args = ap.parse_args()

    paths = [Path(p) for p in args.paths]
    records = _load_records(paths)
    if not records:
        print("No records found. Point at a directory containing run_*.json files.")
        return 1

    result = grade(records)
    graded = result["graded"]

    print(f"Loaded {len(records)} records from {', '.join(str(p) for p in paths)}.")
    _print_accuracy(graded)
    _print_per_prompt(graded)
    _print_behavior(graded)
    _print_by_suite(graded)

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2))
        print(f"\nWrote graded records to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
