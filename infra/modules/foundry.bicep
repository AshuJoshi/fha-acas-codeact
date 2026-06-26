// Microsoft Foundry — AI Services account + project + model deployment.
//
// Modern Foundry uses a multi-tenant ``Microsoft.CognitiveServices``
// account (``kind: 'AIServices'``) with a project sub-resource. The
// account hosts the model deployments; the project is the agent
// namespace that the agent container's ``FoundryChatClient`` targets via
// ``project_endpoint``.
//
// Endpoint shape:
//   account:  https://${name}.services.ai.azure.com
//   project:  https://${name}.services.ai.azure.com/api/projects/${projectName}
//
// AAD-only auth: ``disableLocalAuth: true`` — the deployer + the project's
// system-assigned MSI get the data-plane roles in rbac.bicep.
//
// Adapted from acas-toolkit/infra/modules/foundry.bicep with model
// defaults updated for FHA's gpt-5.4 baseline.

param location string
param tags object
param accountName string
param projectName string
param modelName string
param modelSkuName string
param modelCapacity int

@description('Model format / provider. ``OpenAI`` covers all GPT and gpt-* family models on Foundry.')
param modelFormat string = 'OpenAI'

@description('Model version. Leave empty to let Foundry pick the latest (rejected for gpt-5-* family).')
param modelVersion string = ''

@description('Resource ID of the Application Insights component to link as a project connection. Empty string = no link (no platform-injected APPLICATIONINSIGHTS_CONNECTION_STRING into the agent container, so the agent will emit no telemetry).')
param appInsightsId string = ''

@description('Connection string for the App Insights component referenced by appInsightsId. Required when appInsightsId is set.')
@secure()
param appInsightsConnectionString string = ''

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: accountName
    allowProjectManagement: true
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: 'fha-acas-codeact Foundry project.'
    displayName: projectName
  }
}

// Model deployment lives at the account scope (not the project). All
// projects under the account share the deployment.
resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: modelName
  sku: {
    name: modelSkuName
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: modelFormat
      name: modelName
      version: empty(modelVersion) ? null : modelVersion
    }
  }
  dependsOn: [
    project
  ]
}

// Link Application Insights as a project ``AppInsights`` connection.
//
// THIS IS THE PIECE THAT MAKES TELEMETRY WORK. Once this connection
// exists on the project, the Foundry hosted-agent platform automatically:
//
//   1. Injects ``APPLICATIONINSIGHTS_CONNECTION_STRING`` into the agent
//      container's environment at boot (which is why agent.yaml MUST NOT
//      try to set it — it's a reserved variable).
//   2. Auto-configures the OpenTelemetry exporter inside the
//      ``ResponsesHostServer`` runtime, so all FoundryChatClient model
//      calls, tool calls, and inbound requests ship to App Insights
//      WITHOUT any application code in agent/main.py. This matches the
//      pattern used by every working FHA agent in the parent
//      foundry-hosted-agents workspace (none of which call
//      configure_azure_monitor explicitly).
//
// If you omit this resource the agent runs fine but emits zero telemetry,
// and scripts/query_appinsights.py will return all-zero row counts.
resource appInsightsConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = if (!empty(appInsightsId)) {
  parent: project
  name: 'appi-${uniqueString(appInsightsId)}'
  properties: {
    category: 'AppInsights'
    target: appInsightsId
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsightsConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsightsId
    }
  }
}

output accountName string = account.name
output accountId string = account.id
output accountEndpoint string = 'https://${account.name}.services.ai.azure.com'
output projectName string = project.name
output projectId string = project.id
output projectPrincipalId string = project.identity.principalId
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
output modelDeploymentName string = modelDeployment.name
