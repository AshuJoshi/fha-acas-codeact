"""Three-case latency probe for FHA + ACAS CodeAct.

Runs the three scenarios the README documents, against a single
pre-provisioned ACAS sandbox so the *sandbox itself is always "warm"*
from FHA's perspective. What varies between calls is whether the FHA
microVM is cold, whether the same Responses-session is reused (which
requires chaining ``previous_response_id``), and whether the sandbox
filesystem retains state from a prior call.

**Why ``previous_response_id`` and NOT ``x-ms-chat-isolation-key``.**
``x-ms-chat-isolation-key`` only gates *access* (who is allowed to
reach a session). For the Responses protocol the only thing that
actually pins subsequent calls to the **same microVM** (same
``/proc/sys/kernel/random/boot_id``, same agent ``pid``) is sending
the prior call's response ``id`` as ``previous_response_id`` in the
next request body. This is verified in the parent workspace's
``docs/SandboxAffinity-TestResults.md`` §C.3–C.4. Without it the
platform is free to route each call to a different microVM, which
brings up a fresh ACAS ``SandboxClient`` handle and (because the
service is keyed on (client-handle, sandbox-id)) hands back a fresh
container — making it *look like* the ACAS FS doesn't persist when in
fact the test was talking to a different container.

* **Case 1 — cold FHA, warm sandbox.** First call: no
  ``previous_response_id``. Foundry provisions / attaches a microVM,
  mounts the agent rootfs, boots ``tini → main.py →
  ResponsesHostServer``, and only then runs the model loop. The ACAS
  sandbox is already alive, so the only ACAS cost is the inner
  ``execute_code`` round-trip. We plant a unique token at
  ``/work/probe.txt`` and capture the response ``id`` for chaining.

* **Case 2 — warm FHA (same session), warm sandbox.** Second call,
  ``previous_response_id`` = Case 1's id. This pins the call to the
  *same* microVM (verified by ``boot_id``) so we get the steady-state
  cost of one model loop + one ACAS exec.

* **Case 3 — sandbox FS retention.** Third call,
  ``previous_response_id`` = Case 2's id (so still the same microVM,
  same agent process, same cached ``SandboxClient`` handle), same
  ``x-acas-sandbox-id``. The agent is asked to read ``/work/probe.txt``
  and echo its contents. If the unique token planted in Case 1 comes
  back verbatim, the ACAS sandbox filesystem persisted across calls
  — which it does as long as we don't accidentally fork off to a
  different microVM mid-test.

The probe also asks the agent to emit ``/proc/sys/kernel/random/boot_id``
on every call so we can prove same-microVM-ness in the summary table
(rather than relying on the platform's promise).

On cold-FHA HTTP 500 (transient SSL cert chain warm-up failure
documented elsewhere in this repo) the probe retries up to 3 times for
each case before giving up. Without the retry, a single transient
failure makes the run uninterpretable.

Usage
-----

    set -a; source .azure/fha-acas-codeact-dev/.env; set +a
    uv run python scripts/three_case_latency.py
    uv run python scripts/three_case_latency.py --gap-s 1

Exit code is non-zero if any case fails after retries, if Case 3's
read-back doesn't contain the token, or if ``boot_id`` differs across
cases (indicates microVM hopped — invalidates the FS-retention test).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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

_BOOT_ID_RE = re.compile(
    r"BOOTID=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


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


def _invoke_with_retry(
    *,
    label: str,
    prompt: str,
    endpoint: str,
    agent: str,
    api_version: str,
    sandbox_id: str,
    chat_id: str,
    timeout: float,
    previous_response_id: str | None,
    max_attempts: int = 3,
    retry_gap_s: float = 3.0,
) -> tuple[float, str | None, str, dict | None]:
    """Invoke once, retry on HTTP-500-class failures. Returns
    (elapsed_ms, error_or_None, text, envelope_or_None).
    """
    print(f"\n[probe] ── {label} ──", file=sys.stderr)
    print(f"[probe]   chat   = {chat_id}", file=sys.stderr)
    print(f"[probe]   prev_resp = {previous_response_id or '-'}", file=sys.stderr)
    print(f"[probe]   prompt = {prompt!r}", file=sys.stderr)

    last_err: str | None = None
    last_text = ""
    last_envelope: dict | None = None
    total_ms = 0.0

    for attempt in range(1, max_attempts + 1):
        t0 = time.monotonic()
        text = ""
        envelope: dict | None = None
        err: str | None = None
        try:
            envelope = _invoke_agent(
                project_endpoint=endpoint,
                agent_name=agent,
                api_version=api_version,
                prompt=prompt,
                sandbox_id=sandbox_id,
                chat_id=chat_id,
                timeout=timeout,
                previous_response_id=previous_response_id,
            )
            text = _extract_text(envelope).strip()
        except SystemExit as ex:
            err = f"SystemExit({ex.code}) — HTTP error (likely cold-FHA SSL 500)"
        except Exception as ex:
            err = f"{type(ex).__name__}: {ex}"
        dt_ms = (time.monotonic() - t0) * 1000.0
        total_ms += dt_ms

        if err is None:
            print(
                f"[probe]   attempt {attempt} ok ({_format_ms(dt_ms)})",
                file=sys.stderr,
            )
            short = text if len(text) < 200 else text[:197] + "..."
            print(f"[probe]   text: {short}", file=sys.stderr)
            return total_ms, None, text, envelope

        last_err = err
        last_text = text
        last_envelope = envelope
        print(
            f"[probe]   attempt {attempt} ERROR ({_format_ms(dt_ms)}): {err}",
            file=sys.stderr,
        )
        if attempt < max_attempts:
            print(
                f"[probe]   retrying in {retry_gap_s}s...",
                file=sys.stderr,
            )
            time.sleep(retry_gap_s)
    return total_ms, last_err, last_text, last_envelope


def _extract_boot_id(text: str) -> str | None:
    m = _BOOT_ID_RE.search(text or "")
    return m.group(1).lower() if m else None


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--gap-s", type=float, default=0.0,
                   help="Sleep this many seconds between successful cases.")
    p.add_argument("--max-attempts", type=int, default=3,
                   help="Max retries per case on HTTP 500 (default: 3).")
    p.add_argument("--retry-gap-s", type=float, default=3.0,
                   help="Seconds between retries within a case (default: 3).")
    p.add_argument("--keep-sandbox", action="store_true",
                   help="Do not delete the sandbox after the run.")
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

    endpoint = _require(args.endpoint, "FOUNDRY_PROJECT_ENDPOINT", "--endpoint")
    subscription_id = _require(args.subscription_id, "ACAS_SUBSCRIPTION_ID", "--subscription-id")
    resource_group = _require(args.resource_group, "ACAS_RESOURCE_GROUP", "--resource-group")
    sandbox_group = _require(args.sandbox_group, "ACAS_SANDBOX_GROUP", "--sandbox-group")

    cred = DefaultAzureCredential()

    # Unique token planted in Case 1, read back in Case 3.
    token = f"FHA-PROBE-TOKEN-{uuid.uuid4().hex}"
    # /work is the executor's persistent working dir on ACAS sandboxes
    # (per acas_toolkit.executor docstring + empirical SDK verification).
    file_path = "/work/probe.txt"

    # chat-isolation-key is unique per run (only gates access). The actual
    # session pin is previous_response_id, chained below.
    chat_id = f"probe-{uuid.uuid4().hex[:12]}"
    print(f"[probe] chat-isolation-key = {chat_id}", file=sys.stderr)
    print(f"[probe] planted token      = {token}", file=sys.stderr)
    print(f"[probe] file path          = {file_path}", file=sys.stderr)

    # Pre-provision the ACAS sandbox so it is ALREADY WARM when Case 1
    # invokes the agent. This is the "warm sandbox" the user asked for.
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
    print(f"[probe] sandbox created in {_format_ms(t_create_ms)}: {sandbox_id}",
          file=sys.stderr)

    # Every prompt also asks the agent to print its FHA microVM boot_id
    # via a shell call to /proc/sys/kernel/random/boot_id — we parse the
    # BOOTID=... line from the reply to prove same-microVM-ness across cases.
    BOOT_PROBE = (
        "Also run a shell command to print 'BOOTID=' followed by the contents "
        "of /proc/sys/kernel/random/boot_id (the FHA microVM boot id) on its "
        "own line in your final reply."
    )

    prompt_case1 = (
        f"Run Python in the sandbox to write the exact text {token!r} "
        f"(no trailing newline, no extra whitespace) to the file {file_path!r}. "
        f"Then read the file back and print its contents. "
        f"Reply with the file's content on its own line. {BOOT_PROBE}"
    )
    prompt_case2 = (
        "Run Python in the sandbox to compute the sum of squares from 1 to 100. "
        f"Reply with only the integer on its own line. {BOOT_PROBE}"
    )
    prompt_case3 = (
        f"Run a shell command in the sandbox: `cat {file_path}`. "
        f"Reply with the file's contents on its own line, verbatim. "
        f"{BOOT_PROBE}"
    )

    results: list[tuple[str, float, str | None, str, dict | None]] = []
    prev_resp_id: str | None = None
    try:
        # ── Case 1 ───────────────────────────────────────────────────
        dt, err, text, env = _invoke_with_retry(
            label="Case 1 — cold FHA, warm sandbox (plant token)",
            prompt=prompt_case1, endpoint=endpoint, agent=args.agent,
            api_version=args.api_version, sandbox_id=sandbox_id,
            chat_id=chat_id, timeout=args.timeout,
            previous_response_id=None,
            max_attempts=args.max_attempts, retry_gap_s=args.retry_gap_s,
        )
        results.append(("Case 1 cold FHA + warm sandbox", dt, err, text, env))
        if env and not err:
            prev_resp_id = env.get("id")
            print(f"[probe]   captured response id = {prev_resp_id}",
                  file=sys.stderr)
        if args.gap_s > 0 and not err:
            time.sleep(args.gap_s)

        # ── Case 2 (chained) ─────────────────────────────────────────
        dt, err, text, env = _invoke_with_retry(
            label="Case 2 — warm FHA, same session, warm sandbox",
            prompt=prompt_case2, endpoint=endpoint, agent=args.agent,
            api_version=args.api_version, sandbox_id=sandbox_id,
            chat_id=chat_id, timeout=args.timeout,
            previous_response_id=prev_resp_id,
            max_attempts=args.max_attempts, retry_gap_s=args.retry_gap_s,
        )
        results.append(("Case 2 warm FHA (chained) + warm sandbox", dt, err, text, env))
        if env and not err:
            prev_resp_id = env.get("id")
            print(f"[probe]   captured response id = {prev_resp_id}",
                  file=sys.stderr)
        if args.gap_s > 0 and not err:
            time.sleep(args.gap_s)

        # ── Case 3 (chained, FS retention) ───────────────────────────
        dt, err, text, env = _invoke_with_retry(
            label="Case 3 — sandbox FS retention (read planted token)",
            prompt=prompt_case3, endpoint=endpoint, agent=args.agent,
            api_version=args.api_version, sandbox_id=sandbox_id,
            chat_id=chat_id, timeout=args.timeout,
            previous_response_id=prev_resp_id,
            max_attempts=args.max_attempts, retry_gap_s=args.retry_gap_s,
        )
        results.append(("Case 3 sandbox FS retention (chained)", dt, err, text, env))
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

    # ── Verify filesystem retention + boot_id stability ──────────────
    case1_text = results[0][3] if len(results) >= 1 else ""
    case3_text = results[2][3] if len(results) >= 3 else ""
    retention_ok = token in case3_text
    case1_ok_with_token = token in case1_text

    boot_ids = [_extract_boot_id(r[3]) for r in results]
    unique_boots = {b for b in boot_ids if b}
    same_microvm = len(unique_boots) == 1 and all(b is not None for b in boot_ids)

    # ── Summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 76, file=sys.stderr)
    print("THREE-CASE LATENCY SUMMARY", file=sys.stderr)
    print("=" * 76, file=sys.stderr)
    print(f"  sandbox pre-provision (ACAS)               : "
          f"{_format_ms(t_create_ms)}", file=sys.stderr)
    print("-" * 76, file=sys.stderr)
    for (label, dt, err, _, _env), boot in zip(results, boot_ids):
        status = "ERROR" if err else "ok   "
        boot_short = (boot[:8] + "…") if boot else "?"
        print(
            f"  [{status}] {label:46s} {_format_ms(dt)}  boot={boot_short}",
            file=sys.stderr,
        )
    print("-" * 76, file=sys.stderr)
    if len(results) >= 2 and not results[0][2] and not results[1][2]:
        cold_minus_warm = results[0][1] - results[1][1]
        pct = (cold_minus_warm / results[0][1]) * 100.0 if results[0][1] > 0 else 0
        print(f"  Case 1 − Case 2 (≈ FHA cold-start cost)     : "
              f"{_format_ms(cold_minus_warm)}   ({pct:+.1f}% of Case 1)",
              file=sys.stderr)
    print("-" * 76, file=sys.stderr)
    print("  Microvm pinning check (boot_id across cases):", file=sys.stderr)
    print(f"    unique boot_ids observed                  : "
          f"{len(unique_boots) if unique_boots else 0}", file=sys.stderr)
    print(f"    all cases on same microVM                  : "
          f"{'YES' if same_microvm else 'NO — chaining broke / microVM hopped'}",
          file=sys.stderr)
    print("  Sandbox FS retention check:", file=sys.stderr)
    print(f"    token planted in Case 1 echoed back        : "
          f"{'YES' if case1_ok_with_token else 'no'}", file=sys.stderr)
    print(f"    token read back in Case 3 (`cat`)          : "
          f"{'YES — FS persisted' if retention_ok else 'NO — FS retention FAILED'}",
          file=sys.stderr)
    print("=" * 76, file=sys.stderr)

    any_error = any(e for _, _, e, _, _ in results)
    if any_error or not retention_ok or not same_microvm:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

