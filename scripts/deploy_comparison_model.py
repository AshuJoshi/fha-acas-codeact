#!/usr/bin/env python3
"""Deploy (or delete) an extra Foundry model for A/B comparison.

This is intentionally **separate from `azd up`**. The primary deploy stays
lean and Microsoft-only; use this script on demand to add a second model to
the *existing* Foundry account so you can compare it against the default
``gpt-5.4`` (e.g. for coding quality).

Defaults deploy **Fireworks GLM-5.3** (catalog name ``FW-GLM-5.3``). A small
registry of known comparison models (``COMPARISON_MODELS``) lets you deploy or
tear down several at once with ``--models`` (e.g. GLM-5.3 + Kimi-K3).

    ⚠️  GLM-5.3 / Kimi via Fireworks are **Non-Microsoft (partner MaaS)
        models**. When you use them, data is shared with Fireworks AI and sent
        outside Microsoft systems, and different compliance/data-handling rules
        apply. See the model card / https://trust.fireworks.ai/ before
        sending sensitive code or data through them.

Deploy parameters were captured empirically against a live account:
  format  = Fireworks        (NOT ``OpenAI`` — that's for gpt-* models)
  sku     = DataZoneStandard (pay-as-you-go; the PTU SKUs are
            GlobalProvisionedManaged / DataZoneProvisionedManaged)
  version = 1
No Marketplace terms-acceptance step was required.

Environment (auto-loaded from the active azd env's ``.azure/<env>/.env``):
  FOUNDRY_ACCOUNT_NAME    Cognitive Services (Foundry) account name
  AZURE_RESOURCE_GROUP    resource group containing the account
  AZURE_SUBSCRIPTION_ID   subscription id

Requires: Azure CLI (``az``) on PATH, logged in, with rights to create
model deployments on the account (the ``azd up`` deployer principal has
these via rbac.bicep's Cognitive Services Contributor grant).

Examples
--------
    # Deploy GLM-5.3 with defaults (capacity 1, about 1000 tokens/min, which is low).
    uv run python scripts/deploy_comparison_model.py

    # Bump throughput for real agent runs (subject to your quota).
    uv run python scripts/deploy_comparison_model.py --capacity 10

    # Deploy the whole comparison set at capacity 10.
    uv run python scripts/deploy_comparison_model.py --models all --capacity 10

    # Deploy just Kimi from the registry.
    uv run python scripts/deploy_comparison_model.py --models kimi-k3 --capacity 10

    # Deploy a different Fireworks model under a custom deployment name (ad-hoc).
    uv run python scripts/deploy_comparison_model.py \\
        --deployment-name qwen3 --model-name FW-Qwen3.6-35B-A3B

    # Per-model capacity in one call (a bare key uses --capacity).
    uv run python scripts/deploy_comparison_model.py \\
        --models gpt-6-astra=200,glm-5.3=100,kimi-k3=100

    # List deployments with request/token limits and subscription quota headroom.
    uv run python scripts/deploy_comparison_model.py --list

    # Save the deployment state before a long run, then confirm nothing changed.
    uv run python scripts/deploy_comparison_model.py --snapshot before.json
    uv run python scripts/deploy_comparison_model.py --check before.json

    # One tiny tool-using prompt per model (needs `uv sync --extra compare`).
    uv run python scripts/deploy_comparison_model.py --smoke --models glm-5.3,gpt-6-astra

    # Tear down the whole comparison set (does not touch gpt-5.4).
    uv run python scripts/deploy_comparison_model.py --models all --delete
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


def _load_azd_env() -> None:
    """Populate os.environ from the active azd environment's .env file.

    Resolution is fully dynamic — no environment name is hard-coded:
    ``AZURE_ENV_NAME`` if exported, else the ``defaultEnvironment`` field in
    ``.azure/config.json``. Existing exported vars always win.
    """
    load_dotenv()

    repo_root = Path(__file__).resolve().parent.parent
    azure_dir = repo_root / ".azure"
    if not azure_dir.is_dir():
        return

    env_name = os.environ.get("AZURE_ENV_NAME")
    if not env_name:
        config_path = azure_dir / "config.json"
        if config_path.is_file():
            try:
                env_name = json.loads(config_path.read_text()).get(
                    "defaultEnvironment"
                )
            except (json.JSONDecodeError, OSError):
                env_name = None
    if not env_name:
        return

    env_file = azure_dir / env_name / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)


_load_azd_env()


# Known comparison models (all pay-as-you-go DataZoneStandard). The key is the
# deployment name you'll pass to the benchmark (`--model <key>`); the value is
# the catalog spec. Add entries here to grow the comparison set.
COMPARISON_MODELS: dict[str, dict[str, str]] = {
    "glm-5.3": {
        "model_name": "FW-GLM-5.3",
        "model_format": "Fireworks",
        "model_version": "1",
        "sku": "DataZoneStandard",
    },
    # Kimi K3 launch (2026-07-28). Only DataZoneStandard is available so far
    # (no provisioned SKUs yet) -- expected for a freshly-launched model.
    "kimi-k3": {
        "model_name": "FW-Kimi-K3",
        "model_format": "Fireworks",
        "model_version": "1",
        "sku": "DataZoneStandard",
    },
    # OpenAI's open-weight release, hosted FIRST-PARTY by Azure (format
    # "OpenAI-OSS", "sold directly by Azure" -> data stays in Microsoft, unlike
    # the Fireworks partner MaaS models). Thematically the key comparison: an
    # open-weight model vs the proprietary gpt-5.4 from the same vendor, AND a
    # governance contrast (first-party open-weight vs partner-hosted open-weight).
    # NOTE: the Fireworks FW-GPT-OSS-120B build is deprecated; use this instead.
    # Qwen was PTU-only on this account, so it is not pay-go comparable.
    "gpt-oss-120b": {
        "model_name": "gpt-oss-120b",
        "model_format": "OpenAI-OSS",
        "model_version": "1",
        "sku": "DataZoneStandard",
    },
    # First-party OpenAI frontier tier, added specifically to test Kimi K3
    # against the actual reference class the launch-week hype compares it to
    # (not just our existing mid-tier lineup). Priced close to Kimi K3 ($3/$15):
    # gpt-5.6-terra is $2.50/$15 standard tier.
    "gpt-5.5": {
        "model_name": "gpt-5.5",
        "model_format": "OpenAI",
        "model_version": "2026-04-24",
        "sku": "GlobalStandard",
    },
    "gpt-5.6-terra": {
        "model_name": "gpt-5.6-terra",
        "model_format": "OpenAI",
        "model_version": "2026-07-09",
        "sku": "GlobalStandard",
    },
    # GPT-6 generation (catalog GA, eastus2). Subscription quota for GlobalStandard
    # is 1000 per model, so deploy these well above the fleet's capacity of 30.
    "gpt-6-astra": {
        "model_name": "gpt-6-astra",
        "model_format": "OpenAI",
        "model_version": "2026-09-03",
        "sku": "GlobalStandard",
    },
    "gpt-6.1-sol": {
        "model_name": "gpt-6.1-sol",
        "model_format": "OpenAI",
        "model_version": "2026-09-29",
        "sku": "GlobalStandard",
    },
    "deepseek-v4.1-flash": {
        "model_name": "FW-DeepSeek-V4.1-Flash",
        "model_format": "Fireworks",
        "model_version": "1",
        "sku": "DataZoneStandard",
    },
    # Microsoft's own first-party reasoning model, Preview lifecycle status on
    # this account as of 2026-08-22. Pay-go GlobalStandard SKU (unlike Nemotron,
    # no PTU-only blocker here).
    "mai-thinking-1": {
        "model_name": "MAI-Thinking-1",
        "model_format": "Microsoft",
        "model_version": "2026-06-01",
        "sku": "GlobalStandard",
    },
    # M3 replaced minimax-m2.5 (that deployment was removed on 2026-10-10).
    "minimax-m3": {
        "model_name": "FW-MiniMax-M3",
        "model_format": "Fireworks",
        "model_version": "1",
        "sku": "DataZoneStandard",
    },
    # First Nvidia-lineage model in the comparison set (distinct training
    # lineage from the OpenAI/Chinese-lab models already covered).
    "nemotron-3-super-120b": {
        "model_name": "FW-Nemotron-3-Super-120B-A12B-BF16",
        "model_format": "Fireworks",
        "model_version": "1",
        "sku": "DataZoneStandard",
    },
}

def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(
            f"{name} is not set. It is auto-loaded from the active azd\n"
            f"environment's .env — run `azd up`/`azd provision` first, or export\n"
            f"AZURE_ENV_NAME / source the env:\n"
            f"    set -a; source .azure/$(azd env get-value AZURE_ENV_NAME)/.env; set +a"
        )
    return val


def _az(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an `az` command, returning the completed process (text mode)."""
    return subprocess.run(
        ["az", *args],
        capture_output=True,
        text=True,
    )


def _deployments_json(account: str, resource_group: str) -> list[dict]:
    proc = _az(
        [
            "cognitiveservices", "account", "deployment", "list",
            "-n", account, "-g", resource_group, "-o", "json",
        ]
    )
    if proc.returncode != 0:
        sys.exit(proc.stderr)
    return json.loads(proc.stdout)


def _quota_rows(account: str, resource_group: str) -> dict[str, tuple[float, float]]:
    """Subscription quota rows for the account's region: name -> (used, limit)."""
    loc = _az(
        ["cognitiveservices", "account", "show", "-n", account, "-g", resource_group,
         "--query", "location", "-o", "tsv"]
    ).stdout.strip()
    proc = _az(["cognitiveservices", "usage", "list", "-l", loc, "-o", "json"])
    if not loc or proc.returncode != 0:
        return {}
    return {
        r["name"]["value"].lower(): (r["currentValue"], r["limit"])
        for r in json.loads(proc.stdout)
    }


def _quota_for(rows: dict[str, tuple[float, float]], dep: dict) -> str:
    # Quota rows are keyed <Family>.<Sku>.<model> and drop the FW- prefix; not every model has one.
    sku = dep["sku"]["name"]
    model = dep["properties"]["model"]["name"]
    base = model[3:] if model.startswith("FW-") else model
    suffix = f".{sku}.{base}".lower()
    for key, (used, limit) in rows.items():
        if key.endswith(suffix):
            return f"{used:g}/{limit:g}"
    return "-"


def _rate_limit(dep: dict, key: str) -> str:
    for rl in dep["properties"].get("rateLimits") or []:
        if rl.get("key") == key:
            return f"{rl.get('count'):g}"
    return "-"


def _list_deployments(account: str, resource_group: str) -> int:
    deps = _deployments_json(account, resource_group)
    quota = _quota_rows(account, resource_group)
    header = ["name", "model", "version", "sku", "cap", "state", "req/min", "tok/min", "quota used/limit"]
    rows = [
        [
            d["name"],
            d["properties"]["model"]["name"],
            d["properties"]["model"]["version"],
            d["sku"]["name"],
            str(d["sku"]["capacity"]),
            d["properties"]["provisioningState"],
            _rate_limit(d, "request"),
            _rate_limit(d, "token"),
            _quota_for(quota, d),
        ]
        for d in sorted(deps, key=lambda x: x["name"])
    ]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    for r in [header, *rows]:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    print("(quota is subscription-wide, so 'used' can exceed this account's share)")
    return 0


def _snapshot(account: str, resource_group: str, path: str) -> int:
    deps = _deployments_json(account, resource_group)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "account": account,
                "resource_group": resource_group,
                "deployments": deps,
            },
            indent=2,
        )
    )
    print(f"[deploy-model] saved {len(deps)} deployments to {out}", file=sys.stderr)
    return 0


def _identity(dep: dict) -> tuple[str, str, str, str, str]:
    """What must not change under a benchmark: the model, its version, SKU and state."""
    m = dep["properties"]["model"]
    return (m["name"], m["version"], m["format"], dep["sku"]["name"], dep["properties"]["provisioningState"])


def _check(account: str, resource_group: str, path: str, only: set[str] | None) -> int:
    """Compare live deployments to a saved snapshot; exit 1 if a model, version, SKU or state changed."""
    saved = {d["name"]: d for d in json.loads(Path(path).read_text())["deployments"]}
    now = {d["name"]: d for d in _deployments_json(account, resource_group)}
    problems: list[str] = []
    notes: list[str] = []
    for name, old in saved.items():
        if only and name not in only:
            continue
        cur = now.get(name)
        if cur is None:
            problems.append(f"{name}: deployment is gone")
            continue
        if _identity(old) != _identity(cur):
            problems.append(f"{name}: {_identity(old)} -> {_identity(cur)}")
            continue
        if old["sku"]["capacity"] != cur["sku"]["capacity"]:
            notes.append(f"{name}: capacity {old['sku']['capacity']} -> {cur['sku']['capacity']}")
        if (old.get("systemData") or {}).get("lastModifiedAt") != (cur.get("systemData") or {}).get("lastModifiedAt"):
            notes.append(f"{name}: modified since the snapshot (model, version, SKU and state unchanged)")
    for name in sorted(set(now) - set(saved)):
        if not only or name in only:
            notes.append(f"{name}: not in the snapshot")
    for line in problems:
        print(f"CHANGED  {line}")
    for line in notes:
        print(f"note     {line}")
    print("OK: no model, version, SKU or state changes" if not problems else f"FAIL: {len(problems)} deployment(s) changed")
    return 1 if problems else 0


SMOKE_PROMPT = (
    "Use execute_code to compute the sum of squares from 1 to 100. Reply with the integer only."
)


def _smoke(targets: list[dict[str, str]], apis: list[str]) -> int:
    """Run one tool-using prompt per model and API through run_local_codeact; PASS if the answer is right."""
    runner = Path(__file__).resolve().parent / "run_local_codeact.py"
    failures = 0
    for spec in targets:
        for api in apis:
            t0 = time.monotonic()
            try:
                proc = subprocess.run(
                    [sys.executable, str(runner), SMOKE_PROMPT, "--api", api,
                     "--model", spec["deployment_name"]],
                    capture_output=True, text=True, timeout=300,
                )
                out = proc.stdout + proc.stderr
                ok = proc.returncode == 0 and re.search(r"338,?350", out) is not None
                why = "" if ok else (
                    next((ln.strip() for ln in out.splitlines() if re.search(r"Error|Unsupported|400|404|429", ln)), "no expected answer")
                )
            except subprocess.TimeoutExpired:
                ok, why = False, "timed out after 300s"
            failures += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'}  {spec['deployment_name']:22} {api:10} {time.monotonic() - t0:5.1f}s  {why[:140]}")
    return 1 if failures else 0


def _deploy_one(
    account: str, resource_group: str, spec: dict[str, str], capacity: int
) -> int:
    """Create one model deployment. Returns the az exit code (0 = ok)."""
    print(
        f"[deploy-model] deploying '{spec['deployment_name']}' "
        f"({spec['model_format']}/{spec['model_name']} v{spec['model_version']}, "
        f"sku={spec['sku']} capacity={capacity}) to {account} ({resource_group})",
        file=sys.stderr,
    )
    proc = _az(
        [
            "cognitiveservices", "account", "deployment", "create",
            "-n", account, "-g", resource_group,
            "--deployment-name", spec["deployment_name"],
            "--model-name", spec["model_name"],
            "--model-version", spec["model_version"],
            "--model-format", spec["model_format"],
            "--sku-name", spec["sku"],
            "--sku-capacity", str(capacity),
        ]
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return proc.returncode
    print(f"[deploy-model] deployed '{spec['deployment_name']}'", file=sys.stderr)
    return 0


def _delete_one(account: str, resource_group: str, deployment_name: str) -> int:
    """Delete one model deployment. Returns the az exit code (0 = ok)."""
    print(
        f"[deploy-model] deleting deployment '{deployment_name}' "
        f"from {account} ({resource_group})",
        file=sys.stderr,
    )
    proc = _az(
        [
            "cognitiveservices", "account", "deployment", "delete",
            "-n", account, "-g", resource_group,
            "--deployment-name", deployment_name,
        ]
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return proc.returncode
    print(f"[deploy-model] deleted '{deployment_name}'", file=sys.stderr)
    return 0


def _resolve_targets(args: argparse.Namespace) -> list[dict[str, str]]:
    """Build the list of model specs to operate on.

    ``--models`` (comma-separated registry keys, or ``all``) takes precedence;
    a key may carry its own capacity as ``key=N``. Otherwise fall back to the
    single ad-hoc model from the individual flags.
    """
    if args.models:
        raw = args.models.strip()
        tokens = (
            list(COMPARISON_MODELS)
            if raw.lower() == "all"
            else [k.strip() for k in raw.split(",") if k.strip()]
        )
        targets: list[dict[str, str]] = []
        for token in tokens:
            key, _, cap = token.partition("=")
            spec = COMPARISON_MODELS.get(key)
            if spec is None:
                sys.exit(
                    f"Unknown model key '{key}'. Known: "
                    f"{', '.join(COMPARISON_MODELS)} (or 'all')."
                )
            if cap and not cap.isdigit():
                sys.exit(f"Capacity for '{key}' must be an integer, got '{cap}'.")
            targets.append({"deployment_name": key, **spec, **({"capacity": cap} if cap else {})})
        return targets
    return [
        {
            "deployment_name": args.deployment_name,
            "model_name": args.model_name,
            "model_format": args.model_format,
            "model_version": args.model_version,
            "sku": args.sku,
        }
    ]


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--deployment-name",
        default="glm-5.3",
        help="Name of the deployment to create/delete (default: glm-5.3). "
        "Ignored when --models is given.",
    )
    p.add_argument(
        "--models",
        help="Comma-separated registry keys to deploy/delete as a set, or 'all'. "
        "A key can set its own capacity as key=N (e.g. glm-5.3=100). "
        f"Known: {', '.join(COMPARISON_MODELS)}. Overrides the single-model flags.",
    )
    p.add_argument(
        "--model-name",
        default="FW-GLM-5.3",
        help="Catalog model name (default: FW-GLM-5.3).",
    )
    p.add_argument(
        "--model-format",
        default="Fireworks",
        help="Model format/publisher (default: Fireworks).",
    )
    p.add_argument(
        "--model-version",
        default="1",
        help="Model version (default: 1).",
    )
    p.add_argument(
        "--sku",
        default="DataZoneStandard",
        help="Deployment SKU (default: DataZoneStandard, pay-as-you-go).",
    )
    p.add_argument(
        "--capacity",
        type=int,
        default=1,
        help="SKU capacity. 1 ≈ 1000 tokens/min; raise for real runs "
        "(subject to quota). Default: 1.",
    )
    p.add_argument(
        "--delete",
        action="store_true",
        help="Delete the deployment instead of creating it.",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List deployments with request/token limits and quota headroom, then exit.",
    )
    p.add_argument(
        "--snapshot",
        metavar="FILE",
        help="Save the full state of every deployment to FILE (JSON), then exit.",
    )
    p.add_argument(
        "--check",
        metavar="FILE",
        help="Compare live deployments to a --snapshot FILE and exit 1 if a model, "
        "version, SKU or state changed. Limited to --models when given.",
    )
    p.add_argument(
        "--smoke",
        action="store_true",
        help="Run one tool-using prompt per --models entry through run_local_codeact "
        "and report PASS/FAIL. Needs `uv sync --extra compare`.",
    )
    p.add_argument(
        "--smoke-api",
        choices=["chat", "responses", "both"],
        default="responses",
        help="API surface(s) for --smoke (default: responses).",
    )
    args = p.parse_args()

    account = _require_env("FOUNDRY_ACCOUNT_NAME")
    resource_group = _require_env("AZURE_RESOURCE_GROUP")

    if args.list:
        return _list_deployments(account, resource_group)
    if args.snapshot:
        return _snapshot(account, resource_group, args.snapshot)
    if args.check:
        only = {t["deployment_name"] for t in _resolve_targets(args)} if args.models else None
        return _check(account, resource_group, args.check, only)
    if args.smoke:
        if not args.models:
            sys.exit("--smoke needs --models (registry keys or 'all').")
        apis = ["chat", "responses"] if args.smoke_api == "both" else [args.smoke_api]
        return _smoke(_resolve_targets(args), apis)

    targets = _resolve_targets(args)
    rc = 0
    if args.delete:
        for spec in targets:
            rc = _delete_one(account, resource_group, spec["deployment_name"]) or rc
    else:
        for spec in targets:
            capacity = int(spec.get("capacity", args.capacity))
            rc = _deploy_one(account, resource_group, spec, capacity) or rc

    _list_deployments(account, resource_group)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
