# Cleanup — purge everything (resources, identities, role assignments)

This is the **complete teardown procedure** for an `fha-acas-codeact`
deployment. It removes:

1. The FHA hosted agent (and all live Responses sessions).
2. The Azure resource group (Foundry account, project, ACR, App Insights,
   Log Analytics workspace, ACAS SandboxGroup, all sandboxes underneath).
3. The Cognitive Services soft-deleted record (so the account name can be
   reused).
4. The Log Analytics workspace's soft-deleted record.
5. All role assignments on every resource above (they live on the
   resources, so they die with them).
6. The **Agent Instance** managed identity (Entra) — auto-GC'd by
   Foundry when the agent is deleted.
7. The **Agent Blueprint** managed identity (Entra) + its backing
   Application registration — **NOT** auto-GC'd; must be deleted by
   hand.
8. The local `azd` environment state on disk.

> **Why this is more than `azd down`.** `azd down` only knows about
> what Bicep deployed. It does NOT:
>
> * Cascade-delete the FHA agent (it lives inside the Foundry account
>   but at a different control plane; `azd down` removes the *account*
>   but the agent must be deleted first if it has live sessions, or the
>   account purge can leave dangling state).
> * Touch Entra (the Blueprint Service Principal + App registration
>   stay behind).
>
> The procedure below handles both manually, in the correct order.

`azd down --purge --force` alone is enough if you do **not** care about:
* live Responses sessions (it will return error from the account delete
  while sessions are active),
* the orphan Entra Blueprint SP + App (zero role assignments, but
  still tenant clutter).

If either of those matters, use the full procedure.

---

## Variables

All commands below assume you are in the repo root and your `azd` env
is loaded:

```bash
cd <repo-root>
unset VIRTUAL_ENV
set -a; source .azure/<env-name>/.env; set +a
```

Loaded variables used:

| var | example |
|---|---|
| `AZURE_RESOURCE_GROUP` | `rg-fha-codeact-fha-acas-codeact-dev` |
| `AZURE_SUBSCRIPTION_ID` | `c3bf4bdb-…` |
| `FOUNDRY_ACCOUNT_NAME` | `aif-fha-sr6hjv2tjsezw` |
| `FOUNDRY_PROJECT_ENDPOINT` | `https://aif-fha-sr6hjv2tjsezw.services.ai.azure.com/api/projects/proj-fha-codeact` |
| `ACAS_SANDBOX_GROUP` | `sg-fha-sr6hjv2tjsezw` |

---

## Step 0 — Capture the pre-purge inventory

Before deleting anything, snapshot what exists. You will need the
**Blueprint principal id** and **App id** in Step 5 (Entra cleanup) —
those are NOT in `azd env list`, only in the Foundry agent record.

```bash
TOKEN=$(az account get-access-token --scope https://ai.azure.com/.default --query accessToken -o tsv)

curl -sS -H "Authorization: Bearer $TOKEN" \
  "$FOUNDRY_PROJECT_ENDPOINT/agents/fha-acas-codeact?api-version=v1" \
  | tee /tmp/agent-record.json \
  | python3 -c "import sys,json; a=json.load(sys.stdin); \
ii=a.get('instance_identity') or {}; \
bi=a.get('blueprint') or a.get('blueprint_identity') or {}; \
print('Instance principal_id =', ii.get('principal_id')); \
print('Instance client_id    =', ii.get('client_id')); \
print('Blueprint principal_id=', bi.get('principal_id')); \
print('Blueprint client_id   =', bi.get('client_id'))"
```

Also list resources in the RG and (separately) the system-assigned
managed identities on the Foundry account + project (those die with
their host resources, but it's useful to confirm later that their role
assignments are gone):

```bash
az resource list -g "$AZURE_RESOURCE_GROUP" \
  --query '[].{name:name,type:type}' -o table

az resource list -g "$AZURE_RESOURCE_GROUP" \
  --query '[?identity!=null].{name:name,principalId:identity.principalId,type:identity.type}' \
  -o table
```

---

## Step 1 — Delete the FHA agent (releases the Agent **Instance** MI)

The agent typically has live Responses sessions (any unfinished call
counts). A plain `DELETE` returns `HTTP 409 conflict` in that case.
Use `force=true` to cascade-delete sessions:

```bash
TOKEN=$(az account get-access-token --scope https://ai.azure.com/.default --query accessToken -o tsv)
curl -sS -w "\nHTTP %{http_code}\n" -X DELETE \
  -H "Authorization: Bearer $TOKEN" \
  "$FOUNDRY_PROJECT_ENDPOINT/agents/fha-acas-codeact?api-version=v1&force=true"
```

Expected response:

```json
{"object":"agent.deleted","name":"fha-acas-codeact","deleted":true}
HTTP 200
```

Verify gone (should return `HTTP 404`):

```bash
curl -sS -w "\nHTTP %{http_code}\n" -H "Authorization: Bearer $TOKEN" \
  "$FOUNDRY_PROJECT_ENDPOINT/agents/fha-acas-codeact?api-version=v1" | tail -3
```

**Side effect:** Foundry GCs the **Agent Instance** Entra service principal
(verified empirically — the SP returns 404 from Graph after this step).
The **Blueprint** SP is NOT GC'd — see Step 5.

---

## Step 2 — `azd down --purge --force`

This deletes the resource group **and** purges Cognitive Services
soft-delete (so the account name is reusable) **and** purges the Log
Analytics workspace soft-delete (so workspace name is reusable):

```bash
azd down --purge --force
```

Expected output (≈2½ minutes total):

```
Purging Log Analytics Workspace: log-<...>
  (✓) Done: Purging Log Analytics Workspace: log-<...>
Deleting resource group rg-<...>
  (✓) Done: Deleted resource group rg-<...>
Purging Cognitive Account: aif-<...>
  (✓) Done: Purging Cognitive Account: aif-<...>
SUCCESS: Your application was removed from Azure in 2 minutes 26 seconds.
```

This also removes the ACAS SandboxGroup (child of the RG) and with it,
all ACAS sandboxes underneath. No data-plane cleanup is needed.

---

## Step 3 — Verify Azure-side cleanup

```bash
# RG should be gone
az group show -n "$AZURE_RESOURCE_GROUP" --query name -o tsv
# → ERROR: (ResourceGroupNotFound) ...

# Cognitive Services soft-delete should be empty for that account name
az cognitiveservices account list-deleted \
  --query "[?name=='$FOUNDRY_ACCOUNT_NAME']" -o json
# → []

# All former principals should have ZERO role assignments anywhere
# (Account SMI, Project SMI, Agent Instance, Agent Blueprint)
for oid in \
    <account-smi-objectid> \
    <project-smi-objectid> \
    <agent-instance-principalid> \
    <agent-blueprint-principalid>; do
  echo "-- $oid --"
  az role assignment list --assignee-object-id "$oid" --all \
    --query 'length(@)' -o tsv
done
# → 0  (for all four)
```

If any of the role-assignment counts is > 0 something granted them
outside the standard `infra/modules/rbac.bicep` flow. Investigate
before claiming a clean tenant.

---

## Step 4 — Verify the Agent **Instance** is GC'd (it should be)

```bash
az rest --method get --url \
  "https://graph.microsoft.com/v1.0/servicePrincipals/<agent-instance-principalid>"
# → ERROR: Not Found ... Resource '<...>' does not exist
```

This is the GOOD outcome — Foundry cleaned it up when you deleted the
agent in Step 1.

---

## Step 5 — Delete the orphan Agent **Blueprint** (manual)

The Blueprint Service Principal + its backing App registration
**persist after** the agent and the entire Foundry account are deleted.
They are harmless (no role assignments by Step 3), but they are tenant
clutter. Delete them.

The Blueprint display name follows the pattern:

    <account-name>-<project-name>-<agent-name>-<short-suffix>-AgentIdentityBlueprint

Example:

    aif-fha-sr6hjv2tjsezw-proj-fha-codeact-fha-acas-codeact-8ed42-AgentIdentityBlueprint

```bash
# Discover the IDs (you should have these from Step 0; if not, search by name)
BLUEPRINT_SP_ID=<from step 0 — principal_id>
BLUEPRINT_APP_ID=<from step 0 — client_id>

# Verify it still exists
az ad app list --filter "appId eq '$BLUEPRINT_APP_ID'" \
  --query '[].{displayName:displayName,id:id,appId:appId}' -o json

# Deleting the App cascades the SP delete (a direct `az ad sp delete`
# typically fails with "Insufficient privileges" — but the cascade
# from `az ad app delete` succeeds with normal user perms).
az ad app delete --id "$BLUEPRINT_APP_ID"

# Confirm cascade
az rest --method get --url \
  "https://graph.microsoft.com/v1.0/servicePrincipals/$BLUEPRINT_SP_ID" 2>&1 | tail -2
# → ERROR: Not Found ...
az ad app list --filter "appId eq '$BLUEPRINT_APP_ID'" --query 'length(@)' -o tsv
# → 0
```

> **Permissions note.** `az ad sp delete --id <sp-objectid>` requires
> Graph `Application.ReadWrite.All` and typically fails for normal
> developer accounts with `ERROR: Insufficient privileges to complete
> the operation.` `az ad app delete --id <app-objectid>` works for the
> *owner* of the app registration (which is whoever ran `azd up`) and
> cascades the SP delete. Use `az ad app delete`.

---

## Step 6 — Clean local `azd` env state

`azd down --purge --force` does NOT remove the local env folder; it
leaves `.azure/<env-name>/` so you can re-deploy with the same
environment name later. To start truly clean:

```bash
# Optional: keep a backup of .env in case you need any URLs / IDs later
cp ".azure/$AZURE_ENV_NAME/.env" "/tmp/$AZURE_ENV_NAME-env-$(date -u +%Y%m%dT%H%M%SZ).bak"

rm -rf ".azure/$AZURE_ENV_NAME"

azd env list
# → NAME      DEFAULT   LOCAL     REMOTE   (no rows)
```

---

## What CAN’T be reverted

* App Insights ingestion that has already been sent to the workspace
  during the run is gone with the workspace. If you wanted to keep it,
  export Kusto queries to a `.kql` file BEFORE Step 2 (e.g. via
  `scripts/query_appinsights.py --json --kusto 'AppTraces ...' > run.json`).
* Container images that were pushed to the ACR are gone with the RG.
  If they're needed, `docker pull` + `docker save` them to a tarball
  BEFORE Step 2.
* The `previous_response_id` chain history is gone with the Foundry
  account purge. Any logs / forensics you want must be exported first.

---

## Full one-liner (script)

For convenience, here is the whole thing as a single block. Edit
the Blueprint IDs after Step 0 if you want it to be hands-off:

```bash
set -a; source .azure/<env-name>/.env; set +a
TOKEN=$(az account get-access-token --scope https://ai.azure.com/.default --query accessToken -o tsv)

# 1. delete agent (force = cascade sessions)
curl -sS -X DELETE -H "Authorization: Bearer $TOKEN" \
  "$FOUNDRY_PROJECT_ENDPOINT/agents/fha-acas-codeact?api-version=v1&force=true"

# 2. azd down (deletes RG + purges Cognitive Services + Log Analytics)
azd down --purge --force

# 3. delete orphan Blueprint Entra App (cascades to SP)
#    grab BLUEPRINT_APP_ID from Step 0 inventory
az ad app delete --id "<BLUEPRINT_APP_ID>"

# 4. remove local azd env folder
rm -rf ".azure/<env-name>"
```

---

## Reference: what an actual run looked like

Captured 2026-06-24 from a real teardown of
`rg-fha-codeact-fha-acas-codeact-dev`. Full log preserved at
`.cleanup-log/cleanup-<timestamp>.log` (also `.envbackup` next to it).

| step | duration | notes |
|---|---|---|
| 1. delete agent (force=true) | < 1s | first attempt without `force` returned 409 |
| 2. `azd down --purge --force` | 2m 26s | LA workspace purge, RG delete, Cog Services purge |
| 3. verify RG / soft-delete / RBAC | < 5s | all clean |
| 4. verify Agent Instance gone | < 1s | Graph 404 (Foundry GC'd it) |
| 5. delete Blueprint App | < 2s | cascaded SP delete |
| 6. remove `.azure/<env>/` | < 1s | local cleanup |

**Total wall time:** ≈ 2m 35s for a clean tenant.
