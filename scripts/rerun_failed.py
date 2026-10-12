#!/usr/bin/env python3
"""Re-run the failed (model, prompt) runs of a finished battery into a separate folder.

    uv run --extra compare python scripts/rerun_failed.py RUN_DIR [--out-dir DIR]

Failures are found with the same rule the summary tool uses (no answer, zero
turns, or a known failure marker). Each one is re-run once with the settings
recorded in RUN_DIR/run_meta.json (API, reasoning effort, pacing), one process
per model, into ``RUN_DIR-rerun/<model>/failNN/``. The original folder is not
touched. ``rerun_manifest.json`` maps every re-run to the original run it
replaces, so the two can be reported separately. Use ``--dry-run`` to list
what would be re-run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compare_reasoning_runs as crr  # noqa: E402
import write_run_meta  # noqa: E402


def find_failures(run_dir: Path) -> list[dict[str, Any]]:
    """Failed runs under run_dir: model, prompt, repeat, original file and error."""
    failures = []
    for file in sorted(run_dir.rglob("run_[0-9]*.json")):
        try:
            rec = json.loads(file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rec, dict) and rec.get("prompt") and crr._is_failure(rec):
            failures.append(
                {
                    "model": rec.get("model"),
                    "prompt": rec["prompt"],
                    "original_repeat": rec.get("repeat"),
                    "original_file": str(file),
                    "original_error": (rec.get("error") or "")[:300] or None,
                }
            )
    return failures


def _settings(run_dir: Path) -> dict[str, Any]:
    meta = run_dir / "run_meta.json"
    return json.loads(meta.read_text()).get("settings", {}) if meta.is_file() else {}


def _rerun_one(model: str, item: dict[str, Any], fail_dir: Path, api: str, effort: str | None, gap_s: str) -> int:
    fail_dir.mkdir(parents=True, exist_ok=True)
    prompts = fail_dir / "prompts.txt"
    prompts.write_text(item["prompt"].strip() + "\n")
    cmd = [
        sys.executable, str(HERE / "compare_models.py"),
        "--models", model, "--prompts", str(prompts), "--repeats", "1",
        "--api", api, "--gap-s", gap_s, "--out-dir", str(fail_dir),
    ]
    if effort:
        cmd += ["--reasoning-effort", effort]
    with (fail_dir / "run.log").open("w") as log:
        return subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir", type=Path)
    p.add_argument("--out-dir", type=Path, help="Default: RUN_DIR-rerun.")
    p.add_argument("--dry-run", action="store_true", help="List the failed runs and exit.")
    args = p.parse_args()

    run_dir = args.run_dir.resolve()
    out_dir = (args.out_dir or run_dir.with_name(run_dir.name + "-rerun")).resolve()
    failures = find_failures(run_dir)
    for f in failures:
        print(f"{f['model']:22} rep {f['original_repeat']} | {f['prompt'][:60]} | {(f['original_error'] or 'no answer')[:50]}")
    print(f"{len(failures)} failed run(s) in {run_dir.name}")
    if args.dry_run or not failures:
        return 0
    if out_dir.exists() and any(out_dir.rglob("run_[0-9]*.json")):
        sys.exit(f"{out_dir} already has results; choose another --out-dir")

    settings = _settings(run_dir)
    api = settings.get("api") or "chat"
    effort = settings.get("reasoning_effort")
    gap_s = str(settings.get("gap_s") or "3")

    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in failures:
        by_model[f["model"]].append(f)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "prompts.txt").write_text("\n".join(sorted({f["prompt"].strip() for f in failures})) + "\n")
    os.environ.update(
        MODELS=",".join(by_model), PARALLEL="1", API=api, EFFORT=effort or "", GAP_S=gap_s,
        REPEATS="1", BATCH_SIZE="1", PROMPTS=f"rerun of {run_dir.name}",
    )
    write_run_meta.main(["start", str(out_dir), "--", "--rerun-of", str(run_dir)])

    manifest: list[dict[str, Any]] = []
    jobs: dict[str, list[tuple[dict[str, Any], Path]]] = {}
    for model, items in by_model.items():
        jobs[model] = []
        for n, item in enumerate(items, 1):
            fail_dir = out_dir / model / f"fail{n:02d}"
            jobs[model].append((item, fail_dir))

    def run_model(model: str) -> list[int]:
        return [_rerun_one(model, item, d, api, effort, gap_s) for item, d in jobs[model]]

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        codes = dict(zip(jobs, pool.map(run_model, jobs)))

    bad = 0
    for model, pairs in jobs.items():
        for (item, fail_dir), rc in zip(pairs, codes[model]):
            files = sorted(fail_dir.glob("run_[0-9]*.json"))
            rec = json.loads(files[0].read_text()) if files else {}
            ok = bool(rec) and rc == 0 and not crr._is_failure(rec)
            bad += 0 if ok else 1
            manifest.append({**item, "rerun_dir": str(fail_dir), "rerun_ok": ok})
            print(f"{'OK  ' if ok else 'FAIL'} {model:22} rep {item['original_repeat']} | {item['prompt'][:50]}")
    (out_dir / "rerun_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    write_run_meta.main(["finish", str(out_dir), "ok" if not bad else "failed"])
    print(f"{len(manifest) - bad}/{len(manifest)} re-runs succeeded; manifest in {out_dir / 'rerun_manifest.json'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
