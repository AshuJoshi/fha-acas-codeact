// =============================================================================
// Role assignments for fha-acas-codeact.
//
// Three principals get roles here:
//
//   1. CALLER PRINCIPAL (you, running ``azd up``):
//      * SandboxGroup Data Owner + Contributor on sandbox group
//          (lets the orchestrator script pre-create sandboxes)
//      * Cognitive Services User on Foundry account
//          (data-plane baseline: call the Responses endpoint + inference)
//      * Azure AI Developer on Foundry account
//          (create/read/update/delete projects, agents, tools, knowledge,
//          connections — the "portal authoring" role)
//      * Cognitive Services Contributor on Foundry account
//          (manage the account itself: create model deployments, edit
//          properties. Together these three are what unblock the Microsoft
//          Foundry portal's authoring surfaces — Agents / Models / Knowledge.
//          Subscription ``Owner`` does NOT help: it grants control-plane
//          ``actions`` only, no Cognitive Services ``dataActions``.)
//      * AcrPush on registry
//          (lets ``azd deploy`` push the agent image)
//      * Log Analytics Reader on workspace
//          (lets ``scripts/query_appinsights.py`` query the workspace-based
//          App Insights tables to verify the agent's OTel exporter is
//          shipping spans)
//
//   2. FOUNDRY PROJECT MI (system-assigned on the project resource):
//      * AcrPull on registry
//          This is the PLATFORM pull path. When Foundry boots the agent
//          microVM it pulls the OCI image as the project MI — NOT as
//          the agent's runtime identity. Without this, agent deployment
//          fails with ``[ImageError] Failed to pull container image``.
//          See reference/fha-docs/Foundry Hosted Agents - Agent Identity,
//          Security & Secrets.md: "ACRPull ... is assigned to the project
//          managed identity, not your agent identity."
//
//   3. AGENT INSTANCE MI (NOT in Bicep — see scripts/grant_agent_roles.sh):
//      * SandboxGroup Data Owner + Contributor on sandbox group
//      * Cognitive Services User on Foundry account
//      The Instance MI is what the agent container's ``DefaultAzureCredential``
//      actually resolves to via IMDS. It's stable per agent name (verified
//      against parent repo's v1, v6, v12). The hook discovers it via
//      ``GET /agents/<name>?api-version=v1`` and grants it the runtime roles.
//
// Don't confuse #2 and #3 — the project MI's only job is image pull. The
// agent's actual code-execution downstream RBAC goes through the Instance MI.
// =============================================================================

targetScope = 'resourceGroup'

param sandboxGroupName string
param foundryAccountName string
param registryName string
param logAnalyticsName string

@description('Object ID of the human or SPN running azd up.')
param callerPrincipalId string

@allowed([ 'User', 'ServicePrincipal' ])
param callerPrincipalType string = 'User'

@description('Object ID of the Foundry project system-assigned MI. Needs AcrPull to pull the agent image.')
param foundryProjectPrincipalId string

// --- Built-in role definition IDs (subscription-scope GUIDs) ---
var roleIds = {
  contributor:           'b24988ac-6180-42a0-ab88-20f7382dd24c'
  sandboxGroupDataOwner: 'c24cf47c-5077-412d-a19c-45202126392c'  // ACAS data-plane (preview)
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
  azureAIDeveloper:      '64702f94-c441-49e6-a78b-ef80e0188fee'
  cognitiveServicesContributor: '25fbc0a9-bd7c-42a3-aa1a-3b75d497ee68'  // Foundry portal authoring (deploy models, manage account)
  acrPush:               '8311e382-0749-4cb8-b61a-304f252e45ec'
  acrPull:               '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  logAnalyticsReader:    '73c42c96-874c-492b-b04d-ab87d138a893'
}

// --- Existing-resource references (lookups, not new deployments) ---
resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' existing = {
  name: sandboxGroupName
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: registryName
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

// =============================================================================
// Caller principal — full E2E access for local testing + push.
// =============================================================================

resource callerSandboxDataOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: sandboxGroup
  name: guid(sandboxGroup.id, callerPrincipalId, roleIds.sandboxGroupDataOwner)
  properties: {
    principalId: callerPrincipalId
    principalType: callerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.sandboxGroupDataOwner)
  }
}

resource callerSandboxContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: sandboxGroup
  name: guid(sandboxGroup.id, callerPrincipalId, roleIds.contributor)
  properties: {
    principalId: callerPrincipalId
    principalType: callerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.contributor)
  }
}

resource callerFoundryUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundryAccount
  name: guid(foundryAccount.id, callerPrincipalId, roleIds.cognitiveServicesUser)
  properties: {
    principalId: callerPrincipalId
    principalType: callerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.cognitiveServicesUser)
  }
}

resource callerFoundryAIDeveloper 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundryAccount
  name: guid(foundryAccount.id, callerPrincipalId, roleIds.azureAIDeveloper)
  properties: {
    principalId: callerPrincipalId
    principalType: callerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.azureAIDeveloper)
  }
}

resource callerFoundryPortalContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundryAccount
  name: guid(foundryAccount.id, callerPrincipalId, roleIds.cognitiveServicesContributor)
  properties: {
    principalId: callerPrincipalId
    principalType: callerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.cognitiveServicesContributor)
  }
}

resource callerAcrPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, callerPrincipalId, roleIds.acrPush)
  properties: {
    principalId: callerPrincipalId
    principalType: callerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.acrPush)
  }
}

resource callerLogAnalyticsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: logAnalytics
  name: guid(logAnalytics.id, callerPrincipalId, roleIds.logAnalyticsReader)
  properties: {
    principalId: callerPrincipalId
    principalType: callerPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.logAnalyticsReader)
  }
}

// =============================================================================
// Foundry project MI — platform image pull only.
// =============================================================================

resource projectAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, foundryProjectPrincipalId, roleIds.acrPull)
  properties: {
    principalId: foundryProjectPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.acrPull)
  }
}
