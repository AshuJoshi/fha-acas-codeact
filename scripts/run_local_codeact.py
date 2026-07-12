#!/usr/bin/env python3
"""Local CodeAct agent + orchestrator — run the agent loop on your laptop while
code executes in an ACA Sandbox (cloud). Purpose: benchmark models (latency,
turns, and the actual code they generate) WITHOUT the FHA deploy cycle.

This is intentionally standalone (it does NOT import agent/main.py) so we don't
touch the working FHA agent. It is *conceptually* the same agent, though:
same instructions, the same acas_toolkit ``execute_code`` / ``run_shell`` tools,
the same ``FoundryChatClient`` and models. A later step can factor the shared
pieces into a common module so the FHA and local agents are byte-identical.

Shape (mirrors the FHA orchestrator/agent split, in one file):
  * ORCHESTRATOR (``main`` / ``run_once``): manages the sandbox (lease/reuse),
    picks the model + prompt, invokes the local agent, captures results, cleans up.
  * LOCAL AGENT (``_run_agent``): builds the AF agent bound to the given sandbox
    and runs the prompt, capturing every tool call (the generated code + result
    + timing) and the final answer.

Env (auto-loaded from the active azd env's ``.azure/<env>/.env``):
  AZURE_AI_PROJECT_ENDPOINT | FOUNDRY_PROJECT_ENDPOINT   Foundry project endpoint
  ACAS_SUBSCRIPTION_ID, ACAS_RESOURCE_GROUP, ACAS_LOCATION, ACAS_SANDBOX_GROUP

Auth: local ``az login`` (AzureCliCredential).

Examples
--------
    # Run a coding prompt on gpt-5.4 (leases a fresh sandbox, deletes it after)
    uv run --extra compare python scripts/run_local_codeact.py \\
        "compute the first 20 fibonacci numbers"

    # Compare a model, keep the sandbox, and write structured metrics to JSON
    uv run --extra compare python scripts/run_local_codeact.py \\
        "sort this list and dedupe: [3,1,2,3,1]" --model glm-5.2 --json /tmp/run.json

    # Reuse a sandbox you already have
    uv run --extra compare python scripts/run_local_codeact.py "show os release" \\
        --sandbox-id <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv


def _load_azd_env() -> None:
    """Populate os.environ from the active azd environment's .env file.

    Resolves the env dynamically: ``AZURE_ENV_NAME`` if exported, else the
    ``defaultEnvironment`` in ``.azure/config.json``. Existing exported vars win.
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
                env_name = json.loads(config_path.read_text()).get("defaultEnvironment")
            except (json.JSONDecodeError, OSError):
                env_name = None
    if not env_name:
        return
    env_file = azure_dir / env_name / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)


_load_azd_env()

from agent_framework import Agent, ChatMiddleware, tool  # noqa: E402
from agent_framework.foundry import FoundryChatClient  # noqa: E402
from azure.identity import AzureCliCredential  # noqa: E402
from pydantic import Field  # noqa: E402

from acas_toolkit import SandboxPool  # noqa: E402
from acas_toolkit.integrations.agent_framework import (  # noqa: E402
    make_execute_code_tool,
    make_run_shell_tool,
)


# Kept in sync with agent/main.py's INSTRUCTIONS for parity. (To be shared via a
# common module later — see run header.)
INSTRUCTIONS = """\
You are a CodeAct agent.

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

DEFAULT_MODEL = "gpt-5.4"
DEFAULT_DISK = "python-3.13"

# Detects package-install activity so its wall time can be reported separately
# from pure code execution (a pip install runs in the sandbox, so it lands in the
# tool bucket, not the model bucket — but it inflates wall time and turns, and it
# reveals whether a model reached for a third-party dependency vs the stdlib).
_INSTALL_RE = re.compile(
    r"(?:^|[;&|]|\b)(?:!\s*)?(?:pip3?|uv|conda|mamba|poetry)\s+(?:pip\s+)?install\b"
    r"|\bapt(?:-get)?\s+install\b"
    r"|\bsubprocess\b.*\bpip\b.*\binstall\b",
    re.IGNORECASE | re.DOTALL,
)


def _looks_like_install(payload: str | None) -> bool:
    return bool(payload and _INSTALL_RE.search(payload))


class _ModelCallTimer(ChatMiddleware):
    """Records the wall-clock latency AND token usage of every model (chat) call.
    The number of calls is the turn count (each model<->tool round-trip plus the
    final answer is one model call)."""

    def __init__(self) -> None:
        self.calls_ms: list[float] = []
        self.calls_usage: list[dict[str, int | None]] = []

    async def process(self, context: Any, call_next: Any) -> None:
        t0 = time.monotonic()
        await call_next()
        self.calls_ms.append((time.monotonic() - t0) * 1000.0)
        # usage_details is a TypedDict (plain dict at runtime), so read by key.
        usage = getattr(getattr(context, "result", None), "usage_details", None) or {}
        self.calls_usage.append(
            {
                "input_tokens": usage.get("input_token_count"),
                "output_tokens": usage.get("output_token_count"),
                "total_tokens": usage.get("total_token_count"),
                "reasoning_tokens": usage.get("reasoning_output_token_count"),
            }
        )


def _result_to_dict(res: Any) -> Any:
    """Normalize a tool result (pydantic ExecResult or str) to JSON-able form."""
    if hasattr(res, "model_dump"):
        try:
            return res.model_dump()
        except Exception:
            pass
    return res if isinstance(res, (str, int, float, bool, type(None))) else str(res)


async def _run_agent(
    *,
    pool: SandboxPool,
    sandbox_id: str,
    model: str,
    prompt: str,
    project_endpoint: str,
) -> dict[str, Any]:
    """LOCAL AGENT: build the agent bound to ``sandbox_id`` and run ``prompt``.

    Captures every tool call (generated code/command + result + wall time) and
    the final answer. Returns a structured record.
    """
    inner_execute = make_execute_code_tool(pool, sandbox_id)
    inner_shell = make_run_shell_tool(pool, sandbox_id)

    tool_calls: list[dict[str, Any]] = []

    @tool(approval_mode="never_require")
    def execute_code(
        code: Annotated[
            str,
            Field(
                description=(
                    "Python 3 source code to execute in the ACA Sandbox. Use "
                    "print() for output. Files and installed packages persist "
                    "across calls in this conversation."
                )
            ),
        ],
        timeout_s: Annotated[
            float | None,
            Field(default=None, description="Optional wall-clock timeout in seconds."),
        ] = None,
    ) -> Any:
        t0 = time.monotonic()
        res = inner_execute(code=code, timeout_s=timeout_s)
        dt_ms = (time.monotonic() - t0) * 1000.0
        tool_calls.append(
            {
                "tool": "execute_code",
                "code": code,
                "timeout_s": timeout_s,
                "is_install": _looks_like_install(code),
                "result": _result_to_dict(res),
                "tool_wall_ms": round(dt_ms, 1),
            }
        )
        return res

    @tool(approval_mode="never_require")
    def run_shell(
        command: Annotated[
            str,
            Field(
                description=(
                    "Shell command to execute in the ACA Sandbox. Use this for "
                    "package installs, filesystem inspection, or non-Python commands."
                )
            ),
        ],
    ) -> Any:
        t0 = time.monotonic()
        res = inner_shell(command=command)
        dt_ms = (time.monotonic() - t0) * 1000.0
        tool_calls.append(
            {
                "tool": "run_shell",
                "command": command,
                "is_install": _looks_like_install(command),
                "result": _result_to_dict(res),
                "tool_wall_ms": round(dt_ms, 1),
            }
        )
        return res

    timer = _ModelCallTimer()
    client = FoundryChatClient(
        project_endpoint=project_endpoint,
        model=model,
        credential=AzureCliCredential(),
    )
    agent = Agent(
        client=client,
        name="LocalCodeActAgent",
        instructions=INSTRUCTIONS,
        tools=[execute_code, run_shell],
        middleware=[timer],
    )

    print(f"[local] model={model} sandbox={sandbox_id}", file=sys.stderr)
    print(f"[local] prompt: {prompt}", file=sys.stderr)
    t0 = time.monotonic()
    result = await agent.run(prompt)
    total_ms = (time.monotonic() - t0) * 1000.0

    answer = getattr(result, "text", None) or str(result)
    model_call_ms = [round(x, 1) for x in timer.calls_ms]
    total_model_ms = round(sum(timer.calls_ms), 1)
    total_tool_ms = round(sum(t["tool_wall_ms"] for t in tool_calls), 1)

    # Token usage (per call + totals) and throughput.
    def _sum_tok(key: str) -> int:
        return sum((u.get(key) or 0) for u in timer.calls_usage)

    input_tokens = _sum_tok("input_tokens")
    output_tokens = _sum_tok("output_tokens")
    total_tokens = _sum_tok("total_tokens") or (input_tokens + output_tokens)
    reasoning_tokens = _sum_tok("reasoning_tokens")
    # Not every provider reports usage (e.g. some third-party Foundry models
    # return all-zero counts). Tag the source so 0 isn't mistaken for "free".
    tokens_source = "reported" if total_tokens else "none"
    # Output-token throughput isolates raw generation speed from prompt size and
    # from how verbose a model chooses to be (ms/token is capacity/load-robust).
    ms_per_output_token = (
        round(total_model_ms / output_tokens, 2) if output_tokens else None
    )
    output_tokens_per_s = (
        round(output_tokens / (total_model_ms / 1000.0), 1) if total_model_ms else None
    )

    # Install-vs-exec split of the sandbox (tool) bucket.
    install_ms = round(sum(t["tool_wall_ms"] for t in tool_calls if t.get("is_install")), 1)
    exec_ms = round(total_tool_ms - install_ms, 1)
    needed_install = any(t.get("is_install") for t in tool_calls)

    return {
        "model": model,
        "sandbox_id": sandbox_id,
        "prompt": prompt,
        "answer": answer,
        "total_wall_ms": round(total_ms, 1),
        # Wall time with package installs removed — comparable across models
        # regardless of whether one chose a third-party dependency.
        "wall_excl_install_ms": round(total_ms - install_ms, 1),
        # Turns = number of model (chat) round-trips.
        "num_turns": len(model_call_ms),
        "model_call_ms": model_call_ms,
        "total_model_ms": total_model_ms,
        # Token usage.
        "tokens_source": tokens_source,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "reasoning_tokens": reasoning_tokens,
        "ms_per_output_token": ms_per_output_token,
        "output_tokens_per_s": output_tokens_per_s,
        "model_call_usage": timer.calls_usage,
        "num_tool_calls": len(tool_calls),
        "total_tool_ms": total_tool_ms,
        # Sandbox bucket split: package installs vs actual code execution.
        "needed_install": needed_install,
        "install_ms": install_ms,
        "exec_ms": exec_ms,
        # Latency decomposition: wall = model + sandbox/tool + agent-loop overhead.
        "agent_overhead_ms": round(total_ms - total_model_ms - total_tool_ms, 1),
        "tool_calls": tool_calls,
    }


def _print_summary(rec: dict[str, Any]) -> None:
    print("\n" + "=" * 76)
    print("LOCAL CODEACT RUN")
    print("=" * 76)
    print(f"  model            : {rec['model']}")
    print(f"  sandbox          : {rec['sandbox_id']}")
    print(f"  total wall time  : {rec['total_wall_ms'] / 1000:.2f}s")
    print(
        f"  turns (model)    : {rec['num_turns']}  |  model {rec['total_model_ms'] / 1000:.2f}s"
        f"  ({', '.join(f'{m / 1000:.1f}s' for m in rec['model_call_ms'])})"
    )
    tps = rec.get("output_tokens_per_s")
    mpt = rec.get("ms_per_output_token")
    if rec.get("tokens_source") == "reported":
        print(
            f"  tokens           : in {rec.get('input_tokens', 0)}  out {rec.get('output_tokens', 0)}"
            f"  (reasoning {rec.get('reasoning_tokens', 0)})"
            + (f"  |  {tps:.1f} out-tok/s ({mpt:.1f} ms/tok)" if tps else "")
        )
    else:
        print("  tokens           : n/a (provider did not report usage)")
    install_note = ""
    if rec.get("needed_install"):
        install_note = f"  (install {rec.get('install_ms', 0) / 1000:.2f}s + exec {rec.get('exec_ms', 0) / 1000:.2f}s)"
    print(
        f"  tool calls       : {rec['num_tool_calls']}  |  sandbox {rec['total_tool_ms'] / 1000:.2f}s"
        f"{install_note}"
    )
    print(f"  agent overhead   : {rec['agent_overhead_ms'] / 1000:.2f}s")
    print("-" * 76)
    for i, tc in enumerate(rec["tool_calls"], 1):
        payload = tc.get("code") if tc["tool"] == "execute_code" else tc.get("command")
        status = ""
        if isinstance(tc.get("result"), dict):
            status = f" status={tc['result'].get('status')} exit={tc['result'].get('exit_code')}"
        print(f"  [{i}] {tc['tool']}  ({tc['tool_wall_ms']:.0f}ms){status}")
        snippet = (payload or "").strip().splitlines()
        for line in snippet[:8]:
            print(f"        | {line}")
        if len(snippet) > 8:
            print(f"        | ... ({len(snippet) - 8} more lines)")
    print("-" * 76)
    print("  ANSWER:")
    print("   ", (rec["answer"] or "").strip().replace("\n", "\n    "))
    print("=" * 76)


async def run_once(args: argparse.Namespace) -> int:
    """ORCHESTRATOR: manage the sandbox, then invoke the local agent."""
    project_endpoint = (
        os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
        or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    )
    if not project_endpoint:
        sys.exit(
            "AZURE_AI_PROJECT_ENDPOINT / FOUNDRY_PROJECT_ENDPOINT not set. Run "
            "`azd up`/`azd provision` first (values auto-load from .azure/<env>/.env)."
        )

    with SandboxPool.from_env() as pool:
        if args.sandbox_id:
            print(f"[orch] reusing sandbox {args.sandbox_id}", file=sys.stderr)
            rec = await _run_agent(
                pool=pool,
                sandbox_id=args.sandbox_id,
                model=args.model,
                prompt=args.prompt,
                project_endpoint=project_endpoint,
            )
        else:
            t0 = time.monotonic()
            with pool.lease(disk=args.disk) as sandbox_id:
                lease_ms = (time.monotonic() - t0) * 1000.0
                print(f"[orch] leased sandbox {sandbox_id} ({lease_ms:.0f}ms)", file=sys.stderr)
                rec = await _run_agent(
                    pool=pool,
                    sandbox_id=sandbox_id,
                    model=args.model,
                    prompt=args.prompt,
                    project_endpoint=project_endpoint,
                )
                rec["sandbox_lease_ms"] = round(lease_ms, 1)
                if args.keep_sandbox:
                    print(
                        f"[orch] keeping sandbox {sandbox_id} (--keep-sandbox)",
                        file=sys.stderr,
                    )

    _print_summary(rec)
    if args.json:
        Path(args.json).write_text(json.dumps(rec, indent=2))
        print(f"[orch] wrote {args.json}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("prompt", help="Prompt to send to the local agent.")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Model deployment (default: {DEFAULT_MODEL}).")
    p.add_argument("--sandbox-id", help="Reuse an existing sandbox instead of leasing.")
    p.add_argument("--disk", default=DEFAULT_DISK, help=f"Disk image for a leased sandbox (default: {DEFAULT_DISK}).")
    p.add_argument("--keep-sandbox", action="store_true", help="Do not delete a leased sandbox after the run.")
    p.add_argument("--json", help="Write the structured run record to this path.")
    args = p.parse_args()
    return asyncio.run(run_once(args))


if __name__ == "__main__":
    raise SystemExit(main())
