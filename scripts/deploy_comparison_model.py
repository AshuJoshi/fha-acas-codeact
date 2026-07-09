#!/usr/bin/env python3
"""Deploy (or delete) an extra Foundry model for A/B comparison.

This is intentionally **separate from `azd up`**. The primary deploy stays
lean and Microsoft-only; use this script on demand to add a second model to
the *existing* Foundry account so you can compare it against the default
``gpt-5.4`` (e.g. for coding quality).

Defaults deploy **Fireworks GLM-5.2** (catalog name ``FW-GLM-5.2``).

    ⚠️  GLM-5.2 via Fireworks is a **Non-Microsoft (partner MaaS) model**.
        When you use it, data is shared with Fireworks AI and sent outside
        Microsoft systems, and different compliance/data-handling rules
        apply. See the model card / https://trust.fireworks.ai/ before
        sending sensitive code or data through it.

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
    # Deploy GLM-5.2 with defaults (capacity 1 = ~1000 tokens/min — low).
    uv run python scripts/deploy_comparison_model.py

    # Bump throughput for real agent runs (subject to your quota).
    uv run python scripts/deploy_comparison_model.py --capacity 10

    # Deploy a different Fireworks model under a custom deployment name.
    uv run python scripts/deploy_comparison_model.py \\
        --deployment-name qwen3 --model-name FW-Qwen3.6-35B-A3B

    # List current deployments on the account.
    uv run python scripts/deploy_comparison_model.py --list

    # Tear down the comparison deployment (does not touch gpt-5.4).
    uv run python scripts/deploy_comparison_model.py --delete
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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


def _list_deployments(account: str, resource_group: str) -> int:
    proc = _az(
        [
            "cognitiveservices", "account", "deployment", "list",
            "-n", account, "-g", resource_group,
            "--query",
            "[].{name:name, format:properties.model.format, "
            "model:properties.model.name, version:properties.model.version, "
            "sku:sku.name, capacity:sku.capacity, "
            "state:properties.provisioningState}",
            "-o", "table",
        ]
    )
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--deployment-name",
        default="glm-5.2",
        help="Name of the deployment to create/delete (default: glm-5.2).",
    )
    p.add_argument(
        "--model-name",
        default="FW-GLM-5.2",
        help="Catalog model name (default: FW-GLM-5.2).",
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
        help="List current deployments on the account and exit.",
    )
    args = p.parse_args()

    account = _require_env("FOUNDRY_ACCOUNT_NAME")
    resource_group = _require_env("AZURE_RESOURCE_GROUP")

    if args.list:
        return _list_deployments(account, resource_group)

    if args.delete:
        print(
            f"[deploy-model] deleting deployment '{args.deployment_name}' "
            f"from {account} ({resource_group})",
            file=sys.stderr,
        )
        proc = _az(
            [
                "cognitiveservices", "account", "deployment", "delete",
                "-n", account, "-g", resource_group,
                "--deployment-name", args.deployment_name,
            ]
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            return proc.returncode
        print(f"[deploy-model] deleted '{args.deployment_name}'", file=sys.stderr)
        return _list_deployments(account, resource_group)

    print(
        f"[deploy-model] deploying '{args.deployment_name}' "
        f"({args.model_format}/{args.model_name} v{args.model_version}, "
        f"sku={args.sku} capacity={args.capacity}) to "
        f"{account} ({resource_group})",
        file=sys.stderr,
    )
    proc = _az(
        [
            "cognitiveservices", "account", "deployment", "create",
            "-n", account, "-g", resource_group,
            "--deployment-name", args.deployment_name,
            "--model-name", args.model_name,
            "--model-version", args.model_version,
            "--model-format", args.model_format,
            "--sku-name", args.sku,
            "--sku-capacity", str(args.capacity),
        ]
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return proc.returncode

    print(f"[deploy-model] deployed '{args.deployment_name}'", file=sys.stderr)
    return _list_deployments(account, resource_group)


if __name__ == "__main__":
    raise SystemExit(main())
