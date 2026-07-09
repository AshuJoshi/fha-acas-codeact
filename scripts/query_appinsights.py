"""Query Application Insights / Log Analytics for recent agent traces.

After running a timing probe, use this to verify that
``configure_azure_monitor`` in agent/main.py is actually shipping OTel
spans, traces, and logs to the project's App Insights workspace.

Reads ``AZURE_LOG_ANALYTICS_WORKSPACE_CUSTOMER_ID`` from the environment
(written by Bicep into ``.azure/<env>/.env``). The active azd environment's
``.env`` is auto-loaded on import — no manual ``source`` step needed — so
this works from a fresh terminal. This is the Log Analytics workspace's GUID
(its ``customerId`` property), which is what ``LogsQueryClient.query_workspace``
expects — NOT the full ARM resource id
``/subscriptions/.../workspaces/<name>``.

Usage
-----

    uv run python scripts/query_appinsights.py
    uv run python scripts/query_appinsights.py --minutes 60
    uv run python scripts/query_appinsights.py --table AppDependencies
    uv run python scripts/query_appinsights.py --kusto \
        'AppTraces | where TimeGenerated > ago(1h) | take 5'

Requires ``Log Analytics Reader`` (or higher) on the workspace.
``azd up`` does not auto-grant this; if you see a 403, run::

    az role assignment create --assignee $(az ad signed-in-user show --query id -o tsv) \
        --role 'Log Analytics Reader' --scope "$AZURE_LOG_ANALYTICS_WORKSPACE_ID"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from dotenv import load_dotenv


def _load_azd_env() -> None:
    """Populate os.environ from the active azd environment's .env file.

    Discovery is fully dynamic — no environment name is hard-coded:

    1. A plain ``.env`` in the current working directory (if present).
    2. The azd environment's ``.azure/<env>/.env`` file, where ``<env>`` is
       resolved from ``AZURE_ENV_NAME`` if exported, otherwise from the
       ``defaultEnvironment`` field in ``.azure/config.json``.

    Existing (already-exported) environment variables always win, so an
    explicit ``export`` still overrides these values. This removes the need
    to ``source .azure/<env>/.env`` before running.
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
        # override=False → real exported vars take precedence.
        load_dotenv(env_file, override=False)


_load_azd_env()

# Tables that workspace-based Application Insights writes into. Limited to
# the surface we care about — the script will show how many rows are in each
# in the time window before running any user query.
DEFAULT_TABLES = (
    "AppTraces",         # log records (e.g. print, logger.info)
    "AppRequests",       # incoming HTTP requests (FHA POST /responses)
    "AppDependencies",   # outgoing HTTP (FoundryChatClient → Foundry, ACAS)
    "AppExceptions",     # unhandled exceptions
)


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(
            f"{name} is not set. It is auto-loaded from the active azd\n"
            f"environment's .env — run `azd up`/`azd provision` first, or if you\n"
            f"use a non-default env, export AZURE_ENV_NAME or source its .env:\n"
            f"    set -a; source .azure/$(azd env get-value AZURE_ENV_NAME)/.env; set +a"
        )
    return val


def _summary_kql(minutes: int) -> str:
    tables_union = " | union ".join(
        f"({t} | summarize n = count() | extend Table = '{t}')"
        for t in DEFAULT_TABLES
    )
    # Wrap individual table counts so missing tables don't fail the union.
    return (
        f"let win = ago({minutes}m);\n"
        + " | union ".join(
            f"({t} | where TimeGenerated > win | summarize n = count() | extend Table = '{t}')"
            for t in DEFAULT_TABLES
        )
        + " | project Table, n | order by Table asc"
    )


def _per_table_recent_kql(table: str, minutes: int, top: int) -> str:
    return (
        f"{table} "
        f"| where TimeGenerated > ago({minutes}m) "
        f"| order by TimeGenerated desc "
        f"| take {top}"
    )


def _print_table(headers: list[str], rows: list[list[object]]) -> None:
    widths = [len(h) for h in headers]
    str_rows = [[("" if v is None else str(v)) for v in row] for row in rows]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], min(len(cell), 80))
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in str_rows:
        clipped = [c if len(c) <= 80 else c[:77] + "..." for c in row]
        print(fmt.format(*clipped))


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--minutes",
        type=int,
        default=30,
        help="Look-back window in minutes (default: 30).",
    )
    p.add_argument(
        "--table",
        choices=DEFAULT_TABLES,
        help="Show the most recent rows from a specific table.",
    )
    p.add_argument(
        "--top",
        type=int,
        default=15,
        help="When --table is set, how many rows to show (default: 15).",
    )
    p.add_argument(
        "--kusto",
        help="Run a custom Kusto query verbatim (overrides --table).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSON instead of a formatted table.",
    )
    args = p.parse_args()

    workspace_id = _require_env("AZURE_LOG_ANALYTICS_WORKSPACE_CUSTOMER_ID")

    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    client = LogsQueryClient(cred)

    if args.kusto:
        query = args.kusto
        title = "custom query"
    elif args.table:
        query = _per_table_recent_kql(args.table, args.minutes, args.top)
        title = f"{args.table} (last {args.minutes}m, top {args.top})"
    else:
        query = _summary_kql(args.minutes)
        title = f"row counts per table (last {args.minutes}m)"

    print(f"# {title}", file=sys.stderr)
    print(f"# workspace: {workspace_id}", file=sys.stderr)
    print(f"# query:\n{query}\n", file=sys.stderr)

    try:
        resp = client.query_workspace(
            workspace_id=workspace_id,
            query=query,
            timespan=timedelta(minutes=max(args.minutes, 1)),
        )
    except Exception as ex:
        sys.exit(f"query failed: {ex}")

    if resp.status == LogsQueryStatus.PARTIAL:
        print(f"# WARN: partial result: {resp.partial_error}", file=sys.stderr)

    tables = resp.tables if resp.status == LogsQueryStatus.SUCCESS else resp.partial_data
    if not tables:
        print("(no tables in response — empty workspace?)", file=sys.stderr)
        return 0

    if args.json:
        payload = [
            {"columns": t.columns, "rows": [list(r) for r in t.rows]}
            for t in tables
        ]
        print(json.dumps(payload, default=str, indent=2))
        return 0

    for t in tables:
        _print_table(list(t.columns), [list(r) for r in t.rows])
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
