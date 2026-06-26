# CodeAct Pattern with Foundry Hosted Agents and ACA Sandboxes

A standalone sample that deploys a [**Foundry Hosted Agent (FHA)**](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents) which
executes Python code and shell commands inside an [**Azure Container Apps
Sandbox (ACAS)**](https://sandboxes.azure.com) — the "CodeAct" pattern.

The agent is built using the Microsoft Agent Framework (MAF), and is deployed to FHA. It uses the [`acas-toolkit`](https://github.com/AshuJoshi/acas-toolkit) to route `execute_code` and `run_shell` tool calls into a sandbox you own.


![FHA-ACAS-CodeAct](./docs/fha-acas-codeact.drawio.png)



### More Info:

[CodeAct](docs/codeactintro.md)  
[ACA Sandboxes](docs/ACASandboxesIntro.md)


---

## Prerequisites

- **Azure subscription** with quota for:
  - Cognitive Services (Foundry account) in `eastus2` or `westus2`
  - One `gpt-5.4` model deployment (default; configurable — see Bicep params, you can always use a different model)
  - One ACA sandbox group (preview) in your chosen region

- **CLI tooling on your laptop:**
  - [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) — `az login` once
  - [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) (`azd`) — for `azd up`
  - [uv](https://docs.astral.sh/uv/getting-started/installation/) — for host-side Python
- Python **3.12** (uv will fetch it if missing — see `.python-version`)

## Deploy from scratch

```bash
git clone <this-repo>
cd fha-acas-codeact

# 1. Authenticate
az login
azd auth login

# 2. Provision infra + deploy agent (single command)
azd up
```

`azd up` will:

1. Prompt for an environment name (e.g. `dev`), location, and Foundry model choice
2. Provision a fresh resource group containing:
   - ACAS sandbox group
   - Foundry account + project + chosen model deployment
   - Container Registry (for the agent image)
   - User-assigned Managed Identity + role assignments
   - Log Analytics + Application Insights
3. Build the agent container in ACR via remote build (no local Docker needed)
4. Register a new agent version in your Foundry project

See [docs/deploy.md](docs/deploy.md) for the step-by-step breakdown and
troubleshooting.

## Invoke the deployed agent

After `azd up` completes, the post-deploy hook writes the agent endpoint
into `.azure/<env>/.env`. Load it and run the orchestrator:

```bash
# Pull the deployed values into your shell
source .azure/$(azd env get-value AZURE_ENV_NAME)/.env

# Sync the host-side venv (uv reads pyproject.toml + uv.lock)
uv sync

# Run a prompt end-to-end (creates a sandbox, invokes the agent, deletes it)
uv run python scripts/orchestrate_codeact.py \
    "compute the first 20 fibonacci numbers"
```

Expected output: the assistant reports running Python in the sandbox and
prints the list `[0, 1, 1, 2, 3, ..., 4181]`.

### More invocation modes

```bash
# Reuse an existing sandbox (skip create + delete)
uv run python scripts/orchestrate_codeact.py "show /etc/os-release" \
    --sandbox-id <existing-sandbox-id>

# Keep the sandbox around for the next call
uv run python scripts/orchestrate_codeact.py "install requests" \
    --keep-sandbox

# Force a fresh chat session (new per-conversation isolation)
uv run python scripts/orchestrate_codeact.py "import this" --new-session
```

---

## Validate the deployment

Three scripts verify the agent is healthy after `azd up` or `azd deploy`.
Run them in this order — each one is independent, prints its own
results to stdout, and exits non-zero on failure.

### 1. Three-case latency probe (FS retention + same-microVM check)

```bash
uv run python scripts/three_case_latency.py --gap-s 1
```

Chains three calls via `previous_response_id`:

| Case | What it tests |
|---|---|
| 1. Cold FHA, warm sandbox | First call after microVM boot; plants a token in `/work/probe.txt`. |
| 2. Warm FHA, same session | Same microVM, same sandbox; should be ~3–5× faster than case 1. |
| 3. Sandbox FS retention | Re-reads the planted token via `cat /work/probe.txt`. |

Expected: all three cases land on the **same `boot_id`** (FHA microVM
pinning works), token is read back verbatim (sandbox FS persists).
Exits non-zero if the microVM hops mid-session OR if the token
doesn't round-trip.

### 2. Cold-vs-warm latency profile

```bash
uv run python scripts/timing_probe.py --calls 3 --gap-s 1
```

Makes N chained calls and reports:

- Per-call wall-clock latency
- Same-`boot_id` assertion across all calls
- Cold-call vs warm-avg delta (≈ FHA cold-start cost)

Typical numbers: cold 12–25s, warm 5s, delta 7–20s. Exits non-zero
if the microVM hops (warm measurement invalid).

### 3. Telemetry / App Insights check

```bash
uv run python scripts/query_appinsights.py --minutes 10
```

Row counts per table (`AppTraces`, `AppRequests`, `AppDependencies`,
`AppExceptions`) for the last 10 min. Zero rows in `AppTraces` means
telemetry isn't wired — see `infra/modules/foundry.bicep`
`appInsightsConnection` resource. Drill into a table with
`--table AppExceptions --top 5`.

## Repo layout

```
fha-acas-codeact/
├── agent/                    # Code that ships inside the FHA container
│   ├── main.py               # ResponsesHostServer + tool wiring
│   ├── agent.yaml            # Foundry hosted-agent deployment spec
│   ├── agent.manifest.yaml   # Foundry agent manifest (template form)
│   ├── requirements.txt      # Human-readable dep list (source of truth)
│   ├── requirements.lock     # Flat pinned lock (what pip installs in the image)
│   └── Dockerfile
├── infra/                    # Bicep templates for `azd up` (phase 2)
├── scripts/
│   ├── orchestrate_codeact.py     # Single-call smoke test
│   ├── three_case_latency.py      # FS retention + microVM-pinning probe
│   ├── timing_probe.py            # Cold-vs-warm latency profile
│   ├── query_appinsights.py       # Post-deploy telemetry forensics
│   └── grant_agent_roles.sh       # azd postdeploy hook (runtime RBAC)
├── docs/
│   ├── architecture.md
│   ├── deploy.md
│   └── cleanup.md                 # The full teardown procedure
├── pyproject.toml            # Host-side dev deps (orchestrator only)
├── uv.lock
├── .python-version           # 3.12
└── azure.yaml                # azd service definition (phase 2)
```

The container's deps and the orchestrator's deps are intentionally separate
stacks — see [docs/architecture.md](docs/architecture.md) for why.

---

## Tear down

**See [docs/cleanup.md](docs/cleanup.md) for the full procedure.**

`azd down --purge` alone is **not enough**. It leaks:

- The Agent **Blueprint** Entra app registration + service principal
  (persists in your tenant after the resource group is gone).
- A live FHA agent with active Responses sessions causes the Foundry
  account purge to return **HTTP 409 Conflict**.
- The Log Analytics workspace soft-delete may need explicit purging.

`docs/cleanup.md` walks through the 7-step procedure with reasoning,
gotchas, and a verification checklist that confirms everything is
actually gone.


## License

MIT — see [LICENSE](LICENSE).

## Other

> **Expected baseline noise:** one `requests.exceptions.ConnectionError`
> per microVM cold-boot, target host `169.254.169.254:80` (IMDS).
> FHA microVMs don't expose IMDS; identity flows through the Foundry
> token broker. Always present, always benign. Any **other** exception
> type deserves triage.