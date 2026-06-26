"""Timing & session-affinity probe for the FHA+ACAS CodeAct sample.

Sends N back-to-back Responses-API calls against the deployed agent, all
sharing the same ACAS sandbox AND chained via ``previous_response_id``
so every warm call genuinely lands on the same FHA microVM and the same
agent process. Reports per-call wall-clock latency plus a cold-vs-warm
summary and a microVM-stability assertion.

**How session pinning actually works.** ``x-ms-chat-isolation-key`` only
gates *access* (it is the partition key in Header isolation mode). It
does NOT pin the session to a microVM. The only thing that does is
sending the prior call's response ``id`` back as ``previous_response_id``
in the next request body. This probe chains it across every warm call.
Without chaining the platform is free to route each call to a different
microVM — which invalidates a 'warm-call' measurement entirely.

What this measures
------------------

* **Call 1 (cold)**: no ``previous_response_id``. Foundry provisions /
  attaches a microVM, mounts the agent rootfs, boots tini → ``main.py``
  → ``ResponsesHostServer``, acquires an MI token, and only then runs
  the model loop. This is the *first FHA invocation* cost.
* **Call 2..N (warm)**: each sends the previous call's response ``id``
  as ``previous_response_id``. The same microVM (verified by
  ``boot_id``) handles every call — only work is token reuse, model
  loop, one ACAS exec roundtrip. This isolates the *ACAS loop* cost
  from the *FHA cold-start* cost.

Every prompt also asks the agent to echo ``BOOTID=<...>`` from
``/proc/sys/kernel/random/boot_id`` in its reply; the probe parses
those and asserts they are all identical at the end. If they aren't,
the warm-call measurement is meaningless and the probe exits non-zero.

On cold-FHA HTTP 500 (transient SSL cert-chain warm-up failure) each
call retries up to ``--max-attempts`` times before giving up.

What it does NOT measure (and why)
----------------------------------

* Foundry-internal phase breakdown (image pull, microVM boot, model
  inference time): those land in Application Insights once
  ``configure_azure_monitor`` is wired (see agent/main.py and
  agent/agent.yaml). Use ``scripts/query_appinsights.py`` (or the
  Kusto query in docs/timing.md) to inspect them after a run.
* Inner ACAS exec timings: the ``execute_code`` tool returns a
  ``duration_ms`` field which the model includes in its reasoning; that
  is logged by the agent itself, also visible in App Insights.

Usage
-----

    set -a; source .azure/fha-acas-codeact-dev/.env; set +a
    uv run python scripts/timing_probe.py
    uv run python scripts/timing_probe.py --calls 4 \
        --prompt-cold "Compute fibonacci(20)" \
        --prompt-warm "Now compute fibonacci(25)"
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from pathlib import Path

_BOOT_ID_RE = re.compile(
    r"BOOTID=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

BOOT_PROBE_SUFFIX = (
    " Also run a shell command to print 'BOOTID=' followed by the contents "
    "of /proc/sys/kernel/random/boot_id (the FHA microVM boot id) on its "
    "own line in your final reply."
)

# Allow `uv run python scripts/timing_probe.py` to import sibling module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Imported from the sibling orchestrator so the probe stays in lock-step
# with whatever request-shape orchestrate_codeact.py uses.
from orchestrate_codeact import (  # noqa: E402
    DEFAULT_AGENT_NAME,
    DEFAULT_API_VERSION,
    DEFAULT_DISK,
    DEFAULT_REGION,
    _create_sandbox,
    _delete_sandbox,
    _extract_text,
    _invoke_agent,
)

from azure.identity import DefaultAzureCredential  # noqa: E402


def _require(value: str | None, name: str, flag: str) -> str:
    if not value:
        sys.exit(
            f"{name} is not set. Export it (e.g. `source .azure/<env>/.env`) "
            f"or pass {flag}."
        )
    return value


def _format_ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:7.2f}s"
    return f"{ms:7.0f}ms"


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--calls",
        type=int,
        default=3,
        help="Total number of back-to-back calls (default: 3).",
    )
    p.add_argument(
        "--prompt-cold",
        default="Compute the sum of squares from 1 to 100 and return only the integer.",
        help="Prompt sent on the first (cold) call.",
    )
    p.add_argument(
        "--prompt-warm",
        default="Compute fibonacci(20) and return only the integer.",
        help="Prompt sent on each subsequent (warm) call.",
    )
    p.add_argument(
        "--gap-s",
        type=float,
        default=0.0,
        help="Sleep this many seconds between calls (default: 0).",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Retries per call on HTTP 500 / cold-FHA SSL (default: 3).",
    )
    p.add_argument(
        "--retry-gap-s",
        type=float,
        default=3.0,
        help="Seconds between retries within a call (default: 3).",
    )
    p.add_argument(
        "--keep-sandbox",
        action="store_true",
        help="Do not delete the sandbox after the run (default: delete).",
    )
    p.add_argument("--agent", default=os.environ.get("FHA_AGENT_NAME", DEFAULT_AGENT_NAME))
    p.add_argument("--endpoint", default=os.environ.get("FOUNDRY_PROJECT_ENDPOINT"))
    p.add_argument("--api-version", default=DEFAULT_API_VERSION)
    p.add_argument("--subscription-id", default=os.environ.get("ACAS_SUBSCRIPTION_ID"))
    p.add_argument("--resource-group", default=os.environ.get("ACAS_RESOURCE_GROUP"))
    p.add_argument("--sandbox-group", default=os.environ.get("ACAS_SANDBOX_GROUP"))
    p.add_argument("--region", default=os.environ.get("ACAS_LOCATION", DEFAULT_REGION))
    p.add_argument("--disk", default=os.environ.get("ACAS_DISK", DEFAULT_DISK))
    p.add_argument("--timeout", type=float, default=180.0)
    args = p.parse_args()

    if args.calls < 2:
        sys.exit("--calls must be >= 2 (need at least one cold + one warm).")

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

    # One sandbox + one chat-id for the whole run. The chat-id only gates
    # access; the actual microVM/session pin is `previous_response_id`,
    # chained between calls below. Same sandbox-id keeps the ACAS
    # `SandboxClient` handle cached in the agent process so tool-side
    # state (files, installed packages) persists across turns.
    chat_id = f"probe-{uuid.uuid4().hex[:12]}"
    print(f"[probe] chat-isolation-key = {chat_id}", file=sys.stderr)

    t_create0 = time.monotonic()
    sandbox_id = _create_sandbox(
        cred,
        subscription_id=subscription_id,
        resource_group=resource_group,
        sandbox_group=sandbox_group,
        region=args.region,
        disk=args.disk,
    )
    t_create_ms = (time.monotonic() - t_create0) * 1000.0

    call_timings_ms: list[float] = []
    call_outputs: list[str] = []
    call_errors: list[str | None] = []
    call_boot_ids: list[str | None] = []
    prev_resp_id: str | None = None
    try:
        for i in range(args.calls):
            base_prompt = args.prompt_cold if i == 0 else args.prompt_warm
            prompt = base_prompt + BOOT_PROBE_SUFFIX
            label = "cold" if i == 0 else f"warm {i}"
            print(
                f"\n[probe] ── call {i + 1}/{args.calls} ({label}) ──",
                file=sys.stderr,
            )
            print(f"[probe]   prev_resp = {prev_resp_id or '-'}", file=sys.stderr)
            print(f"[probe]   prompt    = {base_prompt!r}", file=sys.stderr)

            err: str | None = None
            text = ""
            envelope: dict | None = None
            total_ms = 0.0
            for attempt in range(1, args.max_attempts + 1):
                t0 = time.monotonic()
                try:
                    envelope = _invoke_agent(
                        project_endpoint=endpoint,
                        agent_name=args.agent,
                        api_version=args.api_version,
                        prompt=prompt,
                        sandbox_id=sandbox_id,
                        chat_id=chat_id,
                        timeout=args.timeout,
                        previous_response_id=prev_resp_id,
                    )
                    text = _extract_text(envelope).strip()
                    err = None
                except SystemExit as ex:
                    err = (
                        f"SystemExit({ex.code}) — HTTP error "
                        f"(likely cold-FHA SSL 500)"
                    )
                except Exception as ex:
                    err = f"{type(ex).__name__}: {ex}"
                dt_ms = (time.monotonic() - t0) * 1000.0
                total_ms += dt_ms
                if err is None:
                    print(
                        f"[probe]   attempt {attempt} ok ({_format_ms(dt_ms)})",
                        file=sys.stderr,
                    )
                    break
                print(
                    f"[probe]   attempt {attempt} ERROR ({_format_ms(dt_ms)}): {err}",
                    file=sys.stderr,
                )
                if attempt < args.max_attempts:
                    print(
                        f"[probe]   retrying in {args.retry_gap_s}s...",
                        file=sys.stderr,
                    )
                    time.sleep(args.retry_gap_s)

            call_timings_ms.append(total_ms)
            call_outputs.append(text)
            call_errors.append(err)

            boot = None
            if not err:
                m = _BOOT_ID_RE.search(text)
                boot = m.group(1).lower() if m else None
                short = text if len(text) < 200 else text[:197] + "..."
                print(f"[probe]   result: {short}", file=sys.stderr)
                if envelope:
                    prev_resp_id = envelope.get("id") or prev_resp_id
                    print(
                        f"[probe]   captured response id = {prev_resp_id}",
                        file=sys.stderr,
                    )
            call_boot_ids.append(boot)

            if args.gap_s > 0 and i < args.calls - 1:
                time.sleep(args.gap_s)
    finally:
        if not args.keep_sandbox:
            _delete_sandbox(
                cred,
                subscription_id=subscription_id,
                resource_group=resource_group,
                sandbox_group=sandbox_group,
                region=args.region,
                sandbox_id=sandbox_id,
            )

    # ── Summary ────────────────────────────────────────────────────
    ok_indexes = [i for i, e in enumerate(call_errors) if e is None]
    ok_timings = [call_timings_ms[i] for i in ok_indexes]
    cold = call_timings_ms[0] if ok_indexes and ok_indexes[0] == 0 else None
    warms = [call_timings_ms[i] for i in ok_indexes if i > 0]
    warm_avg = sum(warms) / len(warms) if warms else None

    unique_boots = {b for b in call_boot_ids if b}
    same_microvm = (
        len(unique_boots) == 1
        and all(b is not None for b in call_boot_ids)
    )

    print("\n" + "=" * 76, file=sys.stderr)
    print("TIMING SUMMARY", file=sys.stderr)
    print("=" * 76, file=sys.stderr)
    print(f"  sandbox create (ACAS)     : {_format_ms(t_create_ms)}", file=sys.stderr)
    for i, (ms, boot) in enumerate(zip(call_timings_ms, call_boot_ids)):
        tag = "cold" if i == 0 else f"warm {i}"
        status = "ERROR" if call_errors[i] else "ok   "
        boot_short = (boot[:8] + "…") if boot else "?"
        print(
            f"  call {i + 1} ({tag:7s}) [{status}] : {_format_ms(ms)}"
            f"  boot={boot_short}",
            file=sys.stderr,
        )
    print("-" * 76, file=sys.stderr)
    if warm_avg is not None and cold is not None:
        saved_ms = cold - warm_avg
        saved_pct = (saved_ms / cold) * 100.0 if cold > 0 else 0.0
        print(f"  warm avg (ok only)        : {_format_ms(warm_avg)}", file=sys.stderr)
        print(
            f"  cold − warm avg           : {_format_ms(saved_ms)}"
            f"   ({saved_pct:+.1f}% of cold)",
            file=sys.stderr,
        )
    else:
        print(
            "  (insufficient ok calls to compute cold/warm delta)",
            file=sys.stderr,
        )
    print("-" * 76, file=sys.stderr)
    print("  MicroVM pinning check (boot_id across calls):", file=sys.stderr)
    print(
        f"    unique boot_ids observed  : {len(unique_boots)}",
        file=sys.stderr,
    )
    print(
        f"    all calls on same microVM : "
        f"{'YES' if same_microvm else 'NO — chaining broke / microVM hopped'}",
        file=sys.stderr,
    )
    print("=" * 76, file=sys.stderr)
    print(
        "  Interpretation: 'cold' includes microVM provision/attach + token"
        " + first model loop;\n  'warm' is the same call against an already-hot"
        " microVM + same ACAS sandbox.\n  Delta ≈ FHA cold-start cost"
        " (image attach + boot + ResponsesHostServer init).",
        file=sys.stderr,
    )
    # Non-zero exit on any error or if microVM hopped (warm measurement is
    # invalid in that case).
    if any(e is not None for e in call_errors):
        return 2
    if not same_microvm:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
