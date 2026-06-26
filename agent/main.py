"""FHA ACAS CodeAct agent.

Wires execute_code and run_shell through the ACAS Toolkit so the model can
execute Python and shell commands inside an ACA Sandbox.

Sandbox selection precedence (per invocation):
  1. Inbound HTTP header ``x-acas-sandbox-id`` (set by external orchestrator).
  2. Process env var ``ACAS_SANDBOX_ID`` (deployment-wide pre-allocation).
  3. Lease a fresh sandbox from a SandboxPool (slow path).
"""

from __future__ import annotations

import asyncio
import atexit
import os
import signal
from contextvars import ContextVar
from typing import TYPE_CHECKING, Annotated, Any, AsyncIterable

from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from acas_toolkit import SandboxPool, SandboxPoolConfig
from acas_toolkit.integrations.agent_framework import (
    make_execute_code_tool,
    make_run_shell_tool,
)
from acas_toolkit.sandbox_factory import make_sandbox_client
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from pydantic import Field

if TYPE_CHECKING:
    from azure.ai.agentserver.responses import ResponseContext
    from azure.ai.agentserver.responses.models import (
        CreateResponse,
        ResponseStreamEvent,
    )

load_dotenv()


class _ManagedIdentityPool(SandboxPool):
    """SandboxPool that opens using DefaultAzureCredential instead of
    AzureCliCredential.

    The standard SandboxPool.open() calls ``az group create`` via subprocess
    and uses AzureCliCredential for ARM operations.  Both paths fail inside an
    FHA container where the az CLI is absent but a platform managed identity
    is available via IMDS.

    This subclass skips the ensure-RG step (the resource group and sandbox
    group are assumed to already exist) and passes DefaultAzureCredential
    explicitly to make_sandbox_client so neither the control-plane GET nor the
    data-plane client ever needs to spawn az.
    """

    def open(self) -> "SandboxPool":
        cfg = self.config
        cred = DefaultAzureCredential(
            exclude_azure_cli_credential=True,
            exclude_interactive_browser_credential=True,
        )
        self._clients = make_sandbox_client(
            subscription_id=cfg.subscription_id,
            resource_group=cfg.resource_group,
            sandbox_group=cfg.sandbox_group,
            credential=cred,
        )
        if cfg.warm_size > 0:
            self._start_warmer()
        return self

INSTRUCTIONS = """\
You are fha-acas-codeact, a Responses hosted agent.

Use run_shell for environment setup, package installation, and file inspection.
Use execute_code for Python 3 code execution.

Rules:
- When the user asks for computation, data transformation, debugging, or code
  execution, prefer using the tools instead of answering from memory.
- Check tool results directly. Do not claim code ran successfully unless the
  tool output shows success.
- The execute_code tool returns structured execution fields including status,
  exit_code, stdout, stderr, and duration_ms. Inspect status first.
- If a tool fails, explain the error briefly and either retry with a targeted
  fix or ask the user for the missing input.
- Sandbox state persists across calls in this conversation, including files and
  installed packages.
"""


# Per-request sandbox ID, set by ``_ContextCapturingHostServer`` before the
# agent dispatches tools. Header name: ``x-acas-sandbox-id``.
_CURRENT_SANDBOX_ID: ContextVar[str | None] = ContextVar(
    "fha_acas_codeact_sandbox_id", default=None
)
SANDBOX_HEADER_NAME = "x-acas-sandbox-id"

# Deployment-wide fallback if no header is provided.
_ENV_SANDBOX_ID: str | None = os.environ.get("ACAS_SANDBOX_ID")

# Process-global state.
_POOL_CM: Any = None
_POOL: SandboxPool | None = None
_LEASE_CM: Any = None  # only used by SLOW PATH
_LEASED_SBX_ID: str | None = None
# Cache of (execute_code, run_shell) tools keyed by sandbox_id so repeated
# invocations against the same caller-owned sandbox skip re-wiring.
_TOOLS_BY_SBX: dict[str, tuple[Any, Any]] = {}


def _cleanup_sandbox() -> None:
    global _POOL_CM, _POOL, _LEASE_CM, _LEASED_SBX_ID
    if _LEASE_CM is not None:
        _LEASE_CM.__exit__(None, None, None)
        _LEASE_CM = None
        _LEASED_SBX_ID = None
    if _POOL_CM is not None:
        _POOL_CM.__exit__(None, None, None)
        _POOL_CM = None
        _POOL = None
    _TOOLS_BY_SBX.clear()


atexit.register(_cleanup_sandbox)


def _ensure_pool() -> SandboxPool:
    """Open the SandboxPool once per process and reuse for all invocations."""
    global _POOL_CM, _POOL
    if _POOL is not None:
        return _POOL
    pool_cm = _ManagedIdentityPool(SandboxPoolConfig.from_env())
    try:
        pool = pool_cm.__enter__()
    except Exception as ex:
        raise RuntimeError(f"Failed to open SandboxPool: {ex}") from ex
    _POOL_CM = pool_cm
    _POOL = pool
    return pool


def _tools_for_sandbox(sbx_id: str) -> tuple[Any, Any]:
    cached = _TOOLS_BY_SBX.get(sbx_id)
    if cached is not None:
        return cached
    pool = _ensure_pool()
    try:
        execute_code_tool = make_execute_code_tool(pool, sbx_id)
        run_shell_tool = make_run_shell_tool(pool, sbx_id)
    except Exception as ex:
        raise RuntimeError(
            f"Failed to create tools for sandbox {sbx_id}: {ex}"
        ) from ex
    _TOOLS_BY_SBX[sbx_id] = (execute_code_tool, run_shell_tool)
    return execute_code_tool, run_shell_tool


def _lease_fresh_sandbox() -> str:
    """Slow path: acquire a brand-new sandbox lease (process-lifetime)."""
    global _LEASE_CM, _LEASED_SBX_ID
    if _LEASED_SBX_ID is not None:
        return _LEASED_SBX_ID

    pool = _ensure_pool()
    disk = os.environ.get("ACAS_DISK", "python-3.13")
    lease_timeout_s = int(os.environ.get("ACAS_LEASE_TIMEOUT_S", "120"))

    def _timeout_handler(signum, frame):
        raise TimeoutError(
            f"Sandbox lease acquisition timed out after {lease_timeout_s} seconds"
        )

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(lease_timeout_s)
    try:
        lease_cm = pool.lease(disk=disk)
        sbx_id = lease_cm.__enter__()
    except Exception as ex:
        signal.alarm(0)
        raise RuntimeError(f"Failed to lease sandbox: {ex}") from ex
    finally:
        signal.alarm(0)

    _LEASE_CM = lease_cm
    _LEASED_SBX_ID = sbx_id
    return sbx_id


def _resolve_sandbox_id() -> str:
    """Pick the sandbox id for the current invocation.

    Precedence: per-request header (ContextVar) > env var > fresh lease.
    """
    hdr = _CURRENT_SANDBOX_ID.get()
    if hdr:
        return hdr
    if _ENV_SANDBOX_ID:
        return _ENV_SANDBOX_ID
    return _lease_fresh_sandbox()


def _ensure_sandbox_tools() -> tuple[Any, Any]:
    sbx_id = _resolve_sandbox_id()
    return _tools_for_sandbox(sbx_id)


@tool(approval_mode="never_require")
def execute_code(
    code: Annotated[
        str,
        Field(
            description=(
                "Python 3 source code to execute in the ACA Sandbox. Use print() "
                "for output. Files and installed packages persist across calls in "
                "this conversation."
            )
        ),
    ],
    timeout_s: Annotated[
        float | None,
        Field(
            default=None,
            description="Optional wall-clock timeout in seconds for the execution.",
        ),
    ] = None,
) -> Any:
    try:
        execute_code_tool, _ = _ensure_sandbox_tools()
        return execute_code_tool(code=code, timeout_s=timeout_s)
    except Exception as ex:
        return {
            "status": "tool_init_error",
            "exit_code": None,
            "stdout": "",
            "stderr": str(ex),
            "duration_ms": 0,
        }


@tool(approval_mode="never_require")
def run_shell(
    command: Annotated[
        str,
        Field(
            description=(
                "Shell command to execute in the ACA Sandbox. Use this for package "
                "installs, filesystem inspection, or non-Python commands."
            )
        ),
    ],
) -> str:
    try:
        _, run_shell_tool = _ensure_sandbox_tools()
        return run_shell_tool(command=command)
    except Exception as ex:
        return f"<error initializing or executing ACAS run_shell: {ex}>"


async def _sandbox_id_carrier(
    inner: "AsyncIterable[ResponseStreamEvent | dict[str, Any]]",
    context: "ResponseContext",
) -> "AsyncIterable[ResponseStreamEvent | dict[str, Any]]":
    """Wrap the inner async iterable so the per-request sandbox id (read from
    the ``x-acas-sandbox-id`` header) is visible via ContextVar for the
    lifetime of this stream, and cleared on exit. The ``async for`` body
    runs in the same Task as tool dispatch, so the ContextVar is visible to
    tool code.
    """
    headers = dict(getattr(context, "client_headers", {}) or {})
    lowered = {k.lower(): v for k, v in headers.items()}
    sbx_id = lowered.get(SANDBOX_HEADER_NAME)
    token = _CURRENT_SANDBOX_ID.set(sbx_id)
    try:
        async for event in inner:
            yield event
    finally:
        try:
            _CURRENT_SANDBOX_ID.reset(token)
        except (ValueError, LookupError):
            pass


class _ContextCapturingHostServer(ResponsesHostServer):
    """Subclass that extracts the per-request ``x-acas-sandbox-id`` header
    from the inbound ``ResponseContext`` and publishes it on a ContextVar
    so the tool callbacks can pick the right sandbox."""

    async def _handle_response(
        self,
        request: "CreateResponse",
        context: "ResponseContext",
        cancellation_signal: asyncio.Event,
    ) -> "AsyncIterable[ResponseStreamEvent | dict[str, Any]]":
        inner = await super()._handle_response(request, context, cancellation_signal)
        return _sandbox_id_carrier(inner, context)


def main() -> None:
    # Telemetry note: we do NOT call configure_azure_monitor() here. Once
    # infra/modules/foundry.bicep wires App Insights as a project
    # 'AppInsights' connection, the Foundry platform auto-injects the
    # connection string into the container env AND auto-configures the
    # OpenTelemetry exporter inside ResponsesHostServer. This mirrors the
    # pattern used by every working hosted agent in the parent
    # foundry-hosted-agents workspace (none of which call
    # configure_azure_monitor in their main.py).

    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )

    agent = Agent(
        client=client,
        instructions=INSTRUCTIONS,
        tools=[execute_code, run_shell],
        default_options={"store": False},
    )

    _ContextCapturingHostServer(agent).run()


if __name__ == "__main__":
    main()
