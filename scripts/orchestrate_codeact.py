#!/usr/bin/env python3
"""End-to-end orchestrator for the fha-acas-codeact Foundry Hosted Agent.

Flow
----
1. (Optional) Pre-create an ACAS sandbox in the configured sandbox group.
2. POST a prompt to the deployed agent's Responses endpoint, passing the
   sandbox id in the ``x-acas-sandbox-id`` header.
3. Print timings + assistant output.
4. (Optional) Delete the sandbox afterward.

Latency win: with a pre-created sandbox header, the agent skips its slow
lease/create path inside main.py and binds straight to the caller-owned
sandbox — the difference is usually 30s+ on the first invocation.

Prerequisites
-------------
- Caller principal (whoever runs this script): must have both
  ``Contributor`` and ``Container Apps SandboxGroup Data Owner`` on the
  sandbox group resource.
- FHA Agent Instance identity (auto-created by Foundry on deploy): must
  have the same two roles so the agent can actually exec into the
  sandbox once the header arrives. The Bicep templates in infra/ wire
  this up automatically.

Environment
-----------
Required:
  FOUNDRY_PROJECT_ENDPOINT   Full Foundry project endpoint URL
  ACAS_SUBSCRIPTION_ID       Subscription containing the sandbox group
  ACAS_RESOURCE_GROUP        RG containing the sandbox group
  ACAS_SANDBOX_GROUP         Sandbox group name

Optional (with sensible defaults):
  ACAS_LOCATION              westus2
  ACAS_DISK                  python-3.13
  FHA_AGENT_NAME             fha-acas-codeact

Examples
--------
    # Default: create sandbox, run prompt, delete sandbox.
    uv run python scripts/orchestrate_codeact.py \\
        "compute the first 20 fibonacci numbers"

    # Reuse a sandbox you already have (skip create+delete).
    uv run python scripts/orchestrate_codeact.py "show /etc/os-release" \\
        --sandbox-id 1dc083f7-982d-460f-ba00-7421e51e172c

    # Create a sandbox but keep it around for the next call.
    uv run python scripts/orchestrate_codeact.py "import sys; print(sys.version)" \\
        --keep-sandbox
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import SandboxGroupClient
from dotenv import load_dotenv


def _load_azd_env() -> None:
    """Populate os.environ from the active azd environment's .env file.

    Discovery is fully dynamic — no environment name is hard-coded:

    1. A plain ``.env`` in the current working directory (if present).
    2. The azd environment's ``.azure/<env>/.env`` file, where ``<env>`` is
       resolved from ``AZURE_ENV_NAME`` if exported, otherwise from the
       ``defaultEnvironment`` field in ``.azure/config.json``.

    Existing (already-exported) environment variables always win, so an
    explicit ``export`` or ``--endpoint`` still overrides these values.
    This removes the need to ``source .azure/<env>/.env`` before running.
    """
    # 1. Local .env in the current working directory (backwards compatible).
    load_dotenv()

    # 2. Locate the azd project root by walking up for a `.azure` directory.
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
        # override=False → real exported vars and local .env take precedence.
        load_dotenv(env_file, override=False)


_load_azd_env()

# Defaults that are safe to bake in (don't reference any specific tenant).
DEFAULT_REGION = "westus2"
DEFAULT_DISK = "python-3.13"
DEFAULT_AGENT_NAME = "fha-acas-codeact"
DEFAULT_API_VERSION = "v1"
SCOPE = "https://ai.azure.com/.default"
FEATURES_HEADER = "HostedAgents=V1Preview"
SANDBOX_HEADER = "x-acas-sandbox-id"


def _data_plane_endpoint(region: str) -> str:
    return f"https://management.{region}.azuredevcompute.io"


def _create_sandbox(
    cred: DefaultAzureCredential,
    *,
    subscription_id: str,
    resource_group: str,
    sandbox_group: str,
    region: str,
    disk: str,
) -> str:
    """Create a fresh ACAS sandbox and return its id."""
    client = SandboxGroupClient(
        endpoint=_data_plane_endpoint(region),
        credential=cred,
        subscription_id=subscription_id,
        resource_group=resource_group,
        sandbox_group=sandbox_group,
    )
    t0 = time.monotonic()
    poller = client.begin_create_sandbox(disk=disk)
    sandbox_client = poller.result()
    dt = (time.monotonic() - t0) * 1000.0
    sbx_id = sandbox_client.sandbox_id
    print(
        f"[orchestrate] created sandbox {sbx_id} ({dt:.0f}ms)",
        file=sys.stderr,
    )
    return sbx_id


def _delete_sandbox(
    cred: DefaultAzureCredential,
    *,
    subscription_id: str,
    resource_group: str,
    sandbox_group: str,
    region: str,
    sandbox_id: str,
) -> None:
    client = SandboxGroupClient(
        endpoint=_data_plane_endpoint(region),
        credential=cred,
        subscription_id=subscription_id,
        resource_group=resource_group,
        sandbox_group=sandbox_group,
    )
    t0 = time.monotonic()
    try:
        client.delete_sandbox(sandbox_id=sandbox_id)
        dt = (time.monotonic() - t0) * 1000.0
        print(
            f"[orchestrate] deleted sandbox {sandbox_id} ({dt:.0f}ms)",
            file=sys.stderr,
        )
    except Exception as ex:
        print(
            f"[orchestrate] WARN: delete_sandbox({sandbox_id}) failed: {ex}",
            file=sys.stderr,
        )


def _build_url(project_endpoint: str, agent_name: str, api_version: str) -> str:
    base = project_endpoint.rstrip("/")
    return (
        f"{base}/agents/{agent_name}/endpoint/protocols/openai/responses"
        f"?api-version={api_version}"
    )


def _invoke_agent(
    *,
    project_endpoint: str,
    agent_name: str,
    api_version: str,
    prompt: str,
    sandbox_id: str,
    chat_id: str | None,
    timeout: float,
    previous_response_id: str | None = None,
) -> dict[str, Any]:
    cred = DefaultAzureCredential()
    token = cred.get_token(SCOPE).token
    url = _build_url(project_endpoint, agent_name, api_version)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Foundry-Features": FEATURES_HEADER,
        SANDBOX_HEADER: sandbox_id,
    }
    if chat_id:
        headers["x-ms-chat-isolation-key"] = chat_id

    body: dict[str, Any] = {"input": prompt}
    if previous_response_id:
        # This is what actually pins the Responses session to the same FHA
        # microVM (same boot_id, same agent pid). The chat-isolation-key
        # only gates ACCESS; it does NOT pin the session.
        body["previous_response_id"] = previous_response_id

    print(f"[orchestrate] POST {url}", file=sys.stderr)
    print(
        f"[orchestrate]   agent={agent_name}  sandbox={sandbox_id}  "
        f"chat={chat_id or '-'}  prev_resp={previous_response_id or '-'}",
        file=sys.stderr,
    )
    t0 = time.monotonic()
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, headers=headers, json=body)
    dt = (time.monotonic() - t0) * 1000.0

    if r.status_code >= 400:
        print(
            f"[orchestrate] HTTP {r.status_code} ({dt:.0f}ms)\n{r.text}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"[orchestrate] response 200 in {dt:.0f}ms", file=sys.stderr)
    return r.json()


def _extract_text(envelope: dict[str, Any]) -> str:
    if isinstance(envelope.get("output_text"), str):
        return envelope["output_text"]
    parts: list[str] = []
    for item in envelope.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content", []) or []:
            if c.get("type") in ("output_text", "text"):
                parts.append(c.get("text", ""))
    return "".join(parts)


def _require(value: str | None, name: str, flag: str) -> str:
    if not value:
        sys.exit(
            f"{name} is not set. Export it (e.g. `source .azure/<env>/.env`) "
            f"or pass {flag}."
        )
    return value


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("prompt", help="User prompt to send to the agent.")
    p.add_argument(
        "--sandbox-id",
        help="Use an existing sandbox; skip create and skip delete.",
    )
    p.add_argument(
        "--keep-sandbox",
        action="store_true",
        help="Do not delete the sandbox after the call (default: delete).",
    )
    p.add_argument(
        "--agent",
        default=os.environ.get("FHA_AGENT_NAME", DEFAULT_AGENT_NAME),
    )
    p.add_argument(
        "--endpoint",
        default=os.environ.get("FOUNDRY_PROJECT_ENDPOINT"),
    )
    p.add_argument("--api-version", default=DEFAULT_API_VERSION)
    p.add_argument(
        "--new-session",
        action="store_true",
        help="Mint a random x-ms-chat-isolation-key so the platform spins up a fresh session.",
    )
    p.add_argument(
        "--chat-id",
        help="Use this x-ms-chat-isolation-key (overrides --new-session).",
    )
    p.add_argument(
        "--subscription-id",
        default=os.environ.get("ACAS_SUBSCRIPTION_ID"),
    )
    p.add_argument(
        "--resource-group",
        default=os.environ.get("ACAS_RESOURCE_GROUP"),
    )
    p.add_argument(
        "--sandbox-group",
        default=os.environ.get("ACAS_SANDBOX_GROUP"),
    )
    p.add_argument(
        "--region",
        default=os.environ.get("ACAS_LOCATION", DEFAULT_REGION),
    )
    p.add_argument(
        "--disk",
        default=os.environ.get("ACAS_DISK", DEFAULT_DISK),
    )
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument(
        "--raw",
        action="store_true",
        help="Print the full Responses envelope as JSON instead of just text.",
    )
    args = p.parse_args()

    endpoint = _require(args.endpoint, "FOUNDRY_PROJECT_ENDPOINT", "--endpoint")
    subscription_id = _require(
        args.subscription_id, "ACAS_SUBSCRIPTION_ID", "--subscription-id"
    )
    resource_group = _require(
        args.resource_group, "ACAS_RESOURCE_GROUP", "--resource-group"
    )
    sandbox_group = _require(
        args.sandbox_group, "ACAS_SANDBOX_GROUP", "--sandbox-group"
    )

    cred = DefaultAzureCredential()

    own_sandbox = False
    sandbox_id = args.sandbox_id
    if not sandbox_id:
        sandbox_id = _create_sandbox(
            cred,
            subscription_id=subscription_id,
            resource_group=resource_group,
            sandbox_group=sandbox_group,
            region=args.region,
            disk=args.disk,
        )
        own_sandbox = True

    chat_id = args.chat_id
    if args.new_session and not chat_id:
        chat_id = f"chat-{uuid.uuid4().hex[:12]}"

    try:
        envelope = _invoke_agent(
            project_endpoint=endpoint,
            agent_name=args.agent,
            api_version=args.api_version,
            prompt=args.prompt,
            sandbox_id=sandbox_id,
            chat_id=chat_id,
            timeout=args.timeout,
        )
    finally:
        if own_sandbox and not args.keep_sandbox:
            _delete_sandbox(
                cred,
                subscription_id=subscription_id,
                resource_group=resource_group,
                sandbox_group=sandbox_group,
                region=args.region,
                sandbox_id=sandbox_id,
            )

    if args.raw:
        import json

        print(json.dumps(envelope, indent=2))
    else:
        print(_extract_text(envelope))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
