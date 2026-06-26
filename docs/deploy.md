# Deploy

End-to-end deployment of `fha-acas-codeact` from a clean machine to a
working agent you can invoke. Time to first response: ~10-15 minutes on a
fresh subscription, dominated by ACR remote build (~2 min) and Foundry
model deployment provisioning (~3-5 min).

---

## What gets deployed (and which file owns it)

A single `azd up` provisions one resource group (`rg-fha-codeact-<env>`)
containing five logical pieces. Each maps to a specific Bicep module:

| Logical piece | Why it exists | Bicep module | Key Azure resources |
|---|---|---|---|
| **Foundry account + project + model deployment** | Hosts the LLM the agent talks to. The project's system-assigned MSI doubles as the agent's runtime identity. | [`infra/modules/foundry.bicep`](../infra/modules/foundry.bicep) | `Microsoft.CognitiveServices/accounts` (kind `AIServices`)<br>`.../accounts/projects`<br>`.../accounts/deployments` (`gpt-5.4` by default) |
| **ACAS Sandbox Group** | The pool that ACAS sandboxes get leased from. Every `execute_code` / `run_shell` tool call inside the agent targets a sandbox in this group. | [`infra/modules/sandboxgroup.bicep`](../infra/modules/sandboxgroup.bicep) | `Microsoft.App/sandboxGroups` (preview, `2026-02-01-preview`) |
| **Azure Container Registry (ACR)** | Hosts the FHA agent's container image. `azd deploy` does a *remote* build inside the registry (no local Docker needed), then Foundry pulls from here when registering a new agent version. | [`infra/modules/registry.bicep`](../infra/modules/registry.bicep) | `Microsoft.ContainerRegistry/registries` (Basic SKU) |
| **Observability** | Log Analytics workspace + workspace-based App Insights. The agent container picks up the connection string via `APPLICATIONINSIGHTS_CONNECTION_STRING` and Agent Framework wires OTel export. | [`infra/modules/monitoring.bicep`](../infra/modules/monitoring.bicep) | `Microsoft.OperationalInsights/workspaces`<br>`Microsoft.Insights/components` |
| **Role assignments (RBAC)** | Three principals get the roles they need. Bicep grants the caller principal (5 roles) and the Foundry project MSI (AcrPull for image pull); a postdeploy hook grants the Agent Instance MI (which doesn't exist until after agent registration). See "Identity flow" below. | [`infra/modules/rbac.bicep`](../infra/modules/rbac.bicep) + [`scripts/grant_agent_roles.sh`](../scripts/grant_agent_roles.sh) | `Microsoft.Authorization/roleAssignments` × 6 (Bicep) + 3 (postdeploy) |

Composition: [`infra/main.bicep`](../infra/main.bicep) is subscription-scope
and creates the resource group; [`infra/resources.bicep`](../infra/resources.bicep)
is RG-scope and wires the five modules together.

### Identity flow — who needs what

Three principals exist at runtime. **Critically, the Foundry project's
system-assigned MSI is NOT the identity the agent container actually
uses for tool calls.** Inside the FHA microVM, `DefaultAzureCredential`
resolves via IMDS to the **Agent Instance MI** — a separate Entra
service principal Foundry mints when an agent name is first registered.
Runtime grants (sandbox group, Cognitive Services User) on the project
MSI are silent no-ops at runtime.

The project MSI *does* however get one Bicep grant: **`AcrPull` on the
container registry**. That's the *platform* image-pull path — when
Foundry boots a new microVM for the agent, it pulls the OCI image as
the project MSI. Without this grant, `azd up` fails at "Polling agent
status" with `[ImageError] Failed to pull container image`. This is the
one place the project MSI matters; the rest of the agent's downstream
RBAC goes through the Instance MI.

The Instance MI is **stable per agent name** (verified empirically — see
"Identity stability" note below). It doesn't exist until *after* the
first `azd deploy`, so Bicep can't grant to it. The flow is therefore
split into two phases:

```
  ┌──────────────────────┐     ┌──────────────────────────┐     ┌──────────────────────────┐
  │  Caller (you)        │     │  Foundry Project MSI     │     │  Agent Instance MI       │
  │  azd up / orchestrate│     │  (system-assigned)       │     │  (stable per agent name; │
  │                      │     │                          │     │   minted on first deploy)│
  └──────────┬───────────┘     └─────────────┬────────────┘     └────────────┬─────────────┘
             │                                │                               │
   Phase 1 — Bicep grants:           Phase 1 — Bicep grants:        Phase 2 — postdeploy hook grants:
             │                       (PLATFORM USE ONLY)                       │
             ▼                                ▼                               ▼
   SandboxGroup Data Owner           AcrPull (registry)             SandboxGroup Data Owner
   + Contributor (sandbox group)                                    + Contributor (sandbox group)
   + Cognitive Services User         Required so Foundry can        + Cognitive Services User
   + Azure AI Developer (Foundry)    pull the agent OCI image       (Foundry account)
   + AcrPush (registry)              when booting the microVM.
                                     Agent code never uses this MI.
   ↑                                 ↑                              ↑
   rbac.bicep                        rbac.bicep                     scripts/grant_agent_roles.sh
                                                                    (azd hooks.postdeploy)
```

**Why this is correct** (verified empirically — see
[parent repo](https://github.com/AshuJoshi/foundry-hosted-agents)'s
`docs/findings/fha-acas-data-plane-hang/` for the full investigation):

1. Inside the agent container, `cat /proc/1/environ` shows
   `FOUNDRY_AGENT_INSTANCE_CLIENT_ID=<guid>` — a GUID distinct from the
   project MSI principal id.
2. The microVM's IMDS endpoint (`IDENTITY_ENDPOINT`) returns tokens for
   the Instance MI, not the project MSI.
3. Token claims show `serviceprincipalname = <instance-mi-name>`.

The postdeploy hook discovers the Instance MI by calling
`GET {project_endpoint}/agents/<name>?api-version=v1` and reading
`.instance_identity.principal_id` — this is the documented Foundry API
for exactly this purpose. See
[`scripts/grant_agent_roles.sh`](../scripts/grant_agent_roles.sh) for
the exact `az rest` + `az role assignment create` sequence.

> **Identity stability** (verified 2026-06-23 against parent repo's
> `fha-acas-codeact` agent at versions 1, 6, and 12):  both the
> `blueprint.principal_id` and `instance_identity.principal_id` are
> **stable per agent name** — they do **not** change when you bump the
> agent version with `azd deploy`. This means the postdeploy hook does
> meaningful work only on the **first** `azd up` (when the agent name is
> registered and Foundry mints the MI pair); subsequent runs find the
> same principal and the `az role assignment create` calls are no-ops.
> Some older parent-repo docs describe the Instance MI as "per-version";
> that was an early/pre-GA observation and is no longer accurate.
>
> If you want even more durable RBAC (e.g. surviving an agent rename),
> federate a UAMI to the **Blueprint** MI via Workload Identity
> Federation. Out of scope for this sample.

### Configuration flow — Bicep → .env → agent.yaml → container

```
infra/main.bicep outputs              ▼
   AZURE_AI_MODEL_DEPLOYMENT_NAME
   FOUNDRY_PROJECT_ENDPOINT             azd writes →  .azure/<env>/.env
   ACAS_SUBSCRIPTION_ID
   ACAS_RESOURCE_GROUP                                       │
   ACAS_SANDBOX_GROUP                                        │ azd substitutes ${...}
   ACAS_LOCATION                                             ▼
   ACAS_DISK
                                                  agent/agent.yaml.environment_variables
                                                  azure.yaml.config.deployments
                                                          │
                                                          │ azd deploy registers
                                                          ▼
                                                  Foundry agent record
                                                  (env vars baked into the deployment)
                                                          │
                                                          │ Foundry boots microVM
                                                          ▼
                                                  agent/main.py
                                                  reads os.environ[...]
```

---

## 1. Prerequisites

- Azure subscription with quota for one `gpt-5.4` model deployment in
  `eastus2` (default) — check via:
  ```bash
  az cognitiveservices usage list -l eastus2 \
      --query "[?contains(name.value,'OpenAI.GlobalStandard.gpt-5')]" -o table
  ```
- Tools: `az`, `azd`, `uv`, `python` (3.12 — `uv` will fetch if missing)
- `az login` and `azd auth login` completed

## 2. First-time provision

```bash
azd up
```

You'll be prompted for:

| Prompt | Suggested value | Notes |
|---|---|---|
| Environment name | `dev` | Drives the RG name (`rg-fha-codeact-<envname>`) and resource suffixes |
| Azure subscription | (your sub) | |
| Location | `westus2` | For the sandbox group, ACR, MI, observability |
| `foundryLocation` param | `eastus2` | Where the Foundry account + model live (gpt-5.4 catalog) |
| `foundryModelName` param | `gpt-5.4` | Change here to swap the underlying model |

What `azd up` does, in order:

1. **Runs Bicep** at subscription scope (`infra/main.bicep`) to create the
   resource group, then deploys the five modules listed in the table
   above into it. At this point the **caller principal** has all the
   roles it needs.
2. **Builds the agent container** in ACR via remote build. The Dockerfile
   does `pip install --no-deps -r requirements.lock` against the 188
   pre-pinned packages — no resolution, no surprises.
3. **Registers a new agent version** in your Foundry project pointing at
   the just-pushed image tag. On the **first** deploy of a given agent
   name, this step also creates the **Agent Blueprint + Instance MI
   pair** (two distinct Entra service principals). On subsequent deploys
   of the same agent name, only a new version is registered — the MI
   pair is reused unchanged. The agent's env vars (from
   [`agent/agent.yaml`](../agent/agent.yaml)) are substituted from
   `.azure/<env>/.env` at this step.
4. **Runs the postdeploy hook**
   ([`scripts/grant_agent_roles.sh`](../scripts/grant_agent_roles.sh)) —
   discovers the Instance MI principal id via the Foundry API and grants
   it `SandboxGroup Data Owner` + `Contributor` on the sandbox group and
   `Cognitive Services User` on the Foundry account. On the first deploy
   this is what unblocks the agent. On subsequent deploys it's a cheap
   no-op (same principal, same assignments). Without this step on first
   deploy, the agent can boot but every tool call 403s (after a ~117s
   silent retry hang).

### Choosing a different model

The defaults (`gpt-5.4` / version `2026-03-05` / `GlobalStandard` / capacity `10`)
work for most subscriptions. To pick something else:

```bash
azd env set AZURE_AI_MODEL_NAME gpt-5-mini
azd env set AZURE_AI_MODEL_VERSION 2025-08-07
# Optional — only override if you need to change SKU or quota:
# azd env set AZURE_AI_MODEL_SKU_NAME GlobalStandard
# azd env set AZURE_AI_MODEL_CAPACITY 30
azd up
```

These map 1:1 to params on [`infra/main.bicep`](../infra/main.bicep) and
flow through to both the Foundry model deployment (via
[`infra/modules/foundry.bicep`](../infra/modules/foundry.bicep)) and the
agent's `AZURE_AI_MODEL_DEPLOYMENT_NAME` env var. The orchestrator script
auto-picks up the deployed model name on its next call — no code changes
needed.

## 3. First invocation

```bash
# Load endpoint + sandbox group coordinates into your shell
source .azure/$(azd env get-value AZURE_ENV_NAME)/.env

# Sync the host-side venv
uv sync

# Smoke test
uv run python scripts/orchestrate_codeact.py \
    "compute the first 20 fibonacci numbers"
```

Expected output (on stderr):
```
[orchestrate] created sandbox <uuid> (~30000ms)
[orchestrate] POST https://<account>.services.ai.azure.com/.../responses
[orchestrate]   agent=fha-acas-codeact  sandbox=<uuid>  chat=-
[orchestrate] response 200 in ~15000ms
[orchestrate] deleted sandbox <uuid> (~1500ms)
```

On stdout: the agent's natural-language reply containing the Fibonacci
list and a note that the code ran in the sandbox.

## 4. Subsequent deploys

When you change `agent/main.py` or any code shipped to the container:

```bash
azd deploy codeact
```

This skips Bicep and only does the container rebuild + agent version
registration. ~2-3 minutes.

When you change Bicep:

```bash
azd provision
```

Bicep is incremental — `azd` calls `az deployment sub create` in
`Incremental` mode, so only changed resources are touched. The sandbox
group, in particular, is preserved across re-provisions.

## 5. Tear down

```bash
azd down --purge
```

`--purge` is important because:

- Foundry accounts have soft-delete and will reserve the name for 72h
  without it.
- ACR has a `--purge` to actually remove the registry.

If you skip `--purge`, you can manually purge later:
```bash
az cognitiveservices account purge \
    -l eastus2 -n <account-name> -g <rg-name>
```

## Troubleshooting

### Build fails with `ResolutionTooDeep: 200000`

The `agent/requirements.lock` is missing or out of date. Regenerate:

```bash
uv pip compile agent/requirements.txt \
    --prerelease=allow \
    --python-version 3.12 \
    --python-platform x86_64-unknown-linux-gnu \
    --no-strip-extras \
    --emit-index-url \
    -o agent/requirements.lock
azd deploy codeact
```

### First invocation hangs ~117 seconds then 403s

The caller principal is missing the `Container Apps SandboxGroup Data Owner`
role on the sandbox group, OR the role assignment hasn't propagated yet
(propagation takes 30–60s; the ACAS SDK silently retries 403 for ~100s
which masks the auth error as a hang).

[`infra/modules/rbac.bicep`](../infra/modules/rbac.bicep) grants this to
the deployer principal automatically as part of `azd up`. If you've
changed identity since `azd up`, or you're invoking from a CI runner that
wasn't the original deployer, re-run:

```bash
azd provision
```

If it just deployed within the last minute, wait 60 seconds and try
again.

### Agent runs but every tool call returns "tool_init_error"

This means the Agent Instance MI is missing the sandbox-group roles.
Most common cause: the postdeploy hook failed silently or wasn't run
(e.g. you ran `azd provision` instead of `azd up` / `azd deploy`).

To run it manually:

```bash
source .azure/$(azd env get-value AZURE_ENV_NAME)/.env
./scripts/grant_agent_roles.sh
```

The script is idempotent — safe to re-run. It prints the discovered
Instance MI principal id, which you can paste into the Azure portal to
verify the assignments landed.

If the script itself fails:

- **`could not fetch agent`** → the agent wasn't deployed. Run `azd deploy`.
- **`agent response did not contain .instance_identity.principal_id`** →
  Foundry returned the agent but no Instance MI yet. Wait 30s
  (provisioning is async) and retry.
- **`az role assignment create` errors with "principal does not exist"**
  → Graph replication lag. Wait 60s and retry; the script's
  `--assignee-object-id` arg minimizes this but doesn't eliminate it.

### After `azd deploy`, the new version's tool calls 403

Unlikely but possible cause: somehow the agent was re-created under a
new name (e.g. you renamed it in `agent/agent.manifest.yaml`) and the
old name's MI no longer has the roles. Re-run the manual grant command
above against the current name.

Note that a normal `azd deploy` against the same agent name **does not**
rotate the Instance MI — the principal id is stable across versions, so
existing role assignments continue to apply. (See "Identity stability"
note above.) If you're seeing 403s on the second deploy and the hook
claims it succeeded, the more likely cause is Azure RBAC propagation
lag from the *first* deploy — wait 60s and retry the failing call.

### 404 on `/agents/<name>` in postdeploy

The `azd` postdeploy hook is looking up an agent name that doesn't match
the deployed one. Check that the `name:` field in `agent/agent.yaml`
matches the service key in `azure.yaml`. The provisioned agent itself is
fine — this is a postdeploy lookup mismatch, not a deployment failure.
