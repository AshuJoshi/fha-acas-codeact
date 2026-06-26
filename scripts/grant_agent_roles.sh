#!/usr/bin/env bash
# =============================================================================
# scripts/grant_agent_roles.sh
#
# Grants the FHA Agent Instance Managed Identity the runtime roles it
# needs to actually do its job:
#   * Container Apps SandboxGroup Data Owner on the sandbox group
#       (so the agent can lease/exec/delete sandboxes)
#   * Contributor on the sandbox group
#       (the control-plane half of the SandboxGroup Data Owner pair)
#   * Cognitive Services User on the Foundry account
#       (so the agent can call the model deployment via the Responses API)
#
# Why this lives outside Bicep:
# -----------------------------
# The Agent Instance MI is a managed identity that Foundry mints when
# ``azd deploy`` first registers an agent by name. It does NOT exist at
# Bicep time, so there's no way to grant to it declaratively.  This
# script:
#   1. Discovers the Instance MI's object id by calling the Foundry
#      agent API: ``GET /agents/<name>?api-version=v1`` returns
#      ``.instance_identity.principal_id``.
#   2. Runs ``az role assignment create`` with --assignee-object-id +
#      --assignee-principal-type ServicePrincipal so the call skips the
#      Graph lookup (which would fail for a freshly-minted MI due to
#      replication delay).
#
# When this runs:
# ---------------
# Wired into ``azure.yaml`` as a project-level ``postdeploy`` hook.
# Runs automatically after every ``azd up`` / ``azd deploy``.  Fully
# idempotent and safe to re-run.
#
# Identity stability:
# -------------------
# Verified empirically (2026-06-23) against versions 1, 6, and 12 of the
# parent repo's ``fha-acas-codeact`` agent: both ``blueprint.principal_id``
# and ``instance_identity.principal_id`` are STABLE across version bumps
# for a given agent name. So this script does meaningful work only on the
# FIRST ``azd up`` (when Foundry mints the MI pair); subsequent runs find
# the same principal and the assignments short-circuit on "already exists".
# We still run it on every deploy as a cheap safety net.
# =============================================================================

set -euo pipefail

: "${FOUNDRY_PROJECT_ENDPOINT:?must be set (Bicep output, normally in .azure/<env>/.env)}"
: "${ACAS_SUBSCRIPTION_ID:?must be set}"
: "${ACAS_RESOURCE_GROUP:?must be set}"
: "${ACAS_SANDBOX_GROUP:?must be set}"
: "${FOUNDRY_ACCOUNT_NAME:?must be set}"

AGENT_NAME="${FHA_AGENT_NAME:-fha-acas-codeact}"

log() { printf '[grant_agent_roles] %s\n' "$*" >&2; }

# -----------------------------------------------------------------------------
# 1. Resolve the Agent Instance MI principal id.
# -----------------------------------------------------------------------------
log "discovering Instance MI for agent '$AGENT_NAME'..."
AGENT_JSON=$(az rest --method get \
    --url "${FOUNDRY_PROJECT_ENDPOINT%/}/agents/${AGENT_NAME}?api-version=v1" \
    --resource "https://ai.azure.com" \
    2>/dev/null || true)

if [ -z "$AGENT_JSON" ]; then
    log "ERROR: could not fetch agent '$AGENT_NAME' from $FOUNDRY_PROJECT_ENDPOINT"
    log "       check that the agent was deployed (it should have been by azd up)"
    exit 1
fi

PRINCIPAL_ID=$(echo "$AGENT_JSON" | jq -r '.instance_identity.principal_id // empty')

if [ -z "$PRINCIPAL_ID" ]; then
    log "ERROR: agent response did not contain .instance_identity.principal_id"
    log "       response was:"
    echo "$AGENT_JSON" | jq . >&2
    exit 1
fi
log "  Instance MI principal id: $PRINCIPAL_ID"

# Also report the Blueprint id (informational only — we don't grant to it).
BLUEPRINT_ID=$(echo "$AGENT_JSON" | jq -r '.blueprint_identity.principal_id // .blueprint.principal_id // empty')
[ -n "$BLUEPRINT_ID" ] && log "  Blueprint MI principal id: $BLUEPRINT_ID (informational)"

# -----------------------------------------------------------------------------
# 2. Build the scopes.
# -----------------------------------------------------------------------------
SBX_SCOPE="/subscriptions/${ACAS_SUBSCRIPTION_ID}/resourceGroups/${ACAS_RESOURCE_GROUP}/providers/Microsoft.App/sandboxGroups/${ACAS_SANDBOX_GROUP}"
ACCT_SCOPE="/subscriptions/${ACAS_SUBSCRIPTION_ID}/resourceGroups/${ACAS_RESOURCE_GROUP}/providers/Microsoft.CognitiveServices/accounts/${FOUNDRY_ACCOUNT_NAME}"

# -----------------------------------------------------------------------------
# 3. Grant. Idempotent — already-existing assignments return cleanly.
# -----------------------------------------------------------------------------
grant() {
    local role="$1"; local scope="$2"; local label="$3"
    log "grant '$role' on $label..."
    # `az role assignment create` returns exit 0 with a body on success and
    # also returns 0 when the assignment already exists (printing a warning).
    # On any other failure, fall back to checking whether the assignment
    # already exists before declaring failure.
    if az role assignment create \
        --assignee-object-id "$PRINCIPAL_ID" \
        --assignee-principal-type ServicePrincipal \
        --role "$role" \
        --scope "$scope" \
        --output none 2>/dev/null
    then
        log "  granted"
    elif az role assignment list \
            --assignee "$PRINCIPAL_ID" \
            --scope "$scope" \
            --role "$role" \
            --query "[].id" -o tsv 2>/dev/null | grep -q .
    then
        log "  already exists (ok)"
    else
        log "  FAILED — see ``az role assignment create`` output for detail"
        return 1
    fi
}

grant "Container Apps SandboxGroup Data Owner" "$SBX_SCOPE"  "sandbox group"
grant "Contributor"                             "$SBX_SCOPE" "sandbox group"
grant "Cognitive Services User"                 "$ACCT_SCOPE" "Foundry account"

log "done. NOTE: role propagation can take 30-60s; if the orchestrator's"
log "      first call hangs ~117s and then 403s, just wait and retry."
