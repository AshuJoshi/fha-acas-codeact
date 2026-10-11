#!/usr/bin/env python3
"""Record what a benchmark run was, so runs can be compared across dates.

    write_run_meta.py start OUT_DIR [-- extra compare_models.py args]
    write_run_meta.py finish OUT_DIR ok|failed

``start`` writes OUT_DIR/run_meta.json from the environment that
run_reasoning_overnight.sh was given (models, API, effort, pacing, repeats,
prompts and their hash) plus the git commit and key package versions;
``finish`` adds the end time and outcome. Per-run records carry their own
``started_at``; this file is the run-level context.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

PACKAGES = (
    "agent-framework-core",
    "agent-framework-foundry",
    "openai",
    "azure-ai-projects",
    "acas-toolkit",
    "azure-containerapps-sandbox",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def start(out_dir: Path, extra_args: list[str]) -> None:
    env = os.environ
    prompts = Path(out_dir / "prompts.txt")
    prompt_text = prompts.read_bytes() if prompts.is_file() else b""
    meta = {
        "label": out_dir.name,
        "started_at": _now(),
        "ended_at": None,
        "status": "running",
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {name: _version(name) for name in PACKAGES},
        "settings": {
            "models": env.get("MODELS") or None,
            "parallel": env.get("PARALLEL", "0") == "1",
            "api": env.get("API", "chat"),
            "reasoning_effort": env.get("EFFORT") or None,
            "gap_s": env.get("GAP_S", "20"),
            "repeats": env.get("REPEATS", "5"),
            "batch_size": env.get("BATCH_SIZE", "5"),
            "extra_args": extra_args,
        },
        "prompts": {
            "source": env.get("PROMPTS", "prompts/reasoning.txt"),
            "count": len([ln for ln in prompt_text.decode().splitlines() if ln.strip()]),
            "sha256": hashlib.sha256(prompt_text).hexdigest() if prompt_text else None,
        },
    }
    (out_dir / "run_meta.json").write_text(json.dumps(meta, indent=2) + "\n")


def finish(out_dir: Path, status: str) -> None:
    path = out_dir / "run_meta.json"
    meta = json.loads(path.read_text()) if path.is_file() else {"label": out_dir.name}
    meta["ended_at"] = _now()
    meta["status"] = status
    path.write_text(json.dumps(meta, indent=2) + "\n")


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "start":
        extra = argv[3:] if len(argv) > 2 and argv[2] == "--" else []
        start(Path(argv[1]), extra)
        return 0
    if len(argv) == 3 and argv[0] == "finish" and argv[2] in ("ok", "failed"):
        finish(Path(argv[1]), argv[2])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
