// =============================================================================
// fha-acas-codeact — main.bicep (subscription scope)
//
// Provisions a fresh resource group containing everything the agent needs
// to run end-to-end:
//
//   ┌─────────────────────────────────────────────────────────────────┐
//   │ rg-fha-codeact-<env>                                             │
//   │                                                                  │
//   │  Foundry account (AI Services)                                   │
//   │   ├─ Project (system-assigned MSI = agent runtime identity)      │
//   │   └─ Model deployment (gpt-5.4 by default)                       │
//   │                                                                  │
//   │  ACAS Sandbox Group  ◀── tools/execute_code targets this         │
//   │                                                                  │
//   │  Container Registry  ◀── azd builds & pushes agent image here    │
//   │                                                                  │
//   │  Log Analytics + App Insights (observability)                    │
//   │                                                                  │
//   │  Role assignments (rbac.bicep):                                  │
//   │   ├─ Deployer principal (you): full E2E access for local testing │
//   │   └─ Foundry project MSI: runtime access (model + sandbox + ACR) │
//   └─────────────────────────────────────────────────────────────────┘
//
// SAFETY: only touches the RG it creates. ``azd down`` cannot remove
// anything outside ``rg-fha-codeact-<env>``.
// =============================================================================

targetScope = 'subscription'

@minLength(1)
@maxLength(32)
@description('Name of the azd environment. Drives the RG name and resource suffixes.')
param environmentName string

@minLength(1)
@description('Primary Azure region. Hosts the sandbox group, ACR, and observability. No default: azd must prompt for this so AZURE_LOCATION gets set in the environment — which the subscription-scoped deployment needs for its own deployment location.')
param location string

@minLength(1)
@description('Region for the Foundry account + project + model deployment. Defaults to eastus2 because gpt-5.4 may not be in every region\'s OpenAI catalog.')
param foundryLocation string = 'eastus2'

@description('Optional override for the resource group name. Default: rg-fha-codeact-<environmentName>.')
param resourceGroupName string = ''

// --- Model parameters (override via `azd env set` before `azd up`) ---

@description('Foundry model deployment name. Becomes ``AZURE_AI_MODEL_DEPLOYMENT_NAME`` for the agent container.')
param foundryModelName string = 'gpt-5.4'

@description('Foundry model version. Required by the CognitiveServices API for gpt-5-* family models.')
param foundryModelVersion string = '2026-03-05'

@description('Foundry model SKU. ``GlobalStandard`` is pay-as-you-go; switch to ``GlobalProvisionedManaged`` only if you have PTU.')
param foundryModelSkuName string = 'GlobalStandard'

@description('Foundry model deployment capacity (TPM units, in thousands). 10 = 10K tokens/min.')
param foundryModelCapacity int = 10

// --- Caller identity (azd auto-populates these) ---

@description('Object ID of the user or service principal running `azd up`. Receives Cognitive Services User + SandboxGroup Data Owner + Contributor + ACR Push for local end-to-end testing.')
param principalId string

@allowed([ 'User', 'ServicePrincipal' ])
@description('Type of principalId. azd sets this automatically.')
param principalType string = 'User'

@description('Tag bag applied to every resource.')
param tags object = {
  'azd-env-name': environmentName
  workload: 'fha-acas-codeact'
}

var effectiveResourceGroupName = empty(resourceGroupName) ? 'rg-fha-codeact-${environmentName}' : resourceGroupName
var resourceToken = toLower(uniqueString(subscription().id, environmentName, effectiveResourceGroupName))

resource rg 'Microsoft.Resources/resourceGroups@2023-07-01' = {
  name: effectiveResourceGroupName
  location: location
  tags: tags
}

module stack 'resources.bicep' = {
  name: 'fha-acas-codeact-stack'
  scope: rg
  params: {
    location: location
    foundryLocation: foundryLocation
    resourceToken: resourceToken
    tags: tags
    foundryModelName: foundryModelName
    foundryModelVersion: foundryModelVersion
    foundryModelSkuName: foundryModelSkuName
    foundryModelCapacity: foundryModelCapacity
    principalId: principalId
    principalType: principalType
  }
}

// -----------------------------------------------------------------------------
// Outputs — these become entries in .azure/<env>/.env after `azd provision`.
//
// The orchestrator script (scripts/orchestrate_codeact.py) reads ACAS_* and
// FOUNDRY_PROJECT_ENDPOINT. The agent container reads AZURE_AI_* and ACAS_*
// (via agent.yaml's environment_variables block, which substitutes ${...}
// against these outputs at `azd deploy` time).
// -----------------------------------------------------------------------------

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = rg.name
output AZURE_SUBSCRIPTION_ID string = subscription().subscriptionId
output AZURE_TENANT_ID string = subscription().tenantId

// Foundry
output FOUNDRY_ACCOUNT_NAME string = stack.outputs.foundryAccountName
output FOUNDRY_ACCOUNT_ID string = stack.outputs.foundryAccountId
output FOUNDRY_PROJECT_ENDPOINT string = stack.outputs.foundryProjectEndpoint
output AZURE_AI_PROJECT_ENDPOINT string = stack.outputs.foundryProjectEndpoint
output AZURE_AI_PROJECT_ID string = stack.outputs.foundryProjectId
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = stack.outputs.foundryModelDeploymentName
output AZURE_AI_MODEL_VERSION string = foundryModelVersion
output AZURE_AI_MODEL_SKU_NAME string = foundryModelSkuName
output AZURE_AI_MODEL_CAPACITY int = foundryModelCapacity

// ACAS
output ACAS_LOCATION string = location
output ACAS_SUBSCRIPTION_ID string = subscription().subscriptionId
output ACAS_RESOURCE_GROUP string = rg.name
output ACAS_SANDBOX_GROUP string = stack.outputs.sandboxGroupName
output ACAS_DISK string = 'python-3.13'

// ACR (consumed by azd for remote build)
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = stack.outputs.acrLoginServer
output AZURE_CONTAINER_REGISTRY_NAME string = stack.outputs.acrName

// Observability
output APPLICATIONINSIGHTS_CONNECTION_STRING string = stack.outputs.appInsightsConnectionString
output APPLICATIONINSIGHTS_NAME string = stack.outputs.appInsightsName
output AZURE_LOG_ANALYTICS_WORKSPACE_NAME string = stack.outputs.logAnalyticsName
output AZURE_LOG_ANALYTICS_WORKSPACE_ID string = stack.outputs.logAnalyticsResourceId
// LogsQueryClient.query_workspace() wants the workspace GUID (customerId),
// not the ARM resource ID. scripts/query_appinsights.py reads this.
output AZURE_LOG_ANALYTICS_WORKSPACE_CUSTOMER_ID string = stack.outputs.logAnalyticsCustomerId
