// =============================================================================
// fha-acas-codeact — resources.bicep (resource-group scope)
//
// Orchestrates the per-module deployments. Splits between vendored modules
// (from the acas-toolkit project) and net-new modules specific to FHA:
//
//   Vendored from acas-toolkit/infra/modules/:
//     sandboxgroup.bicep   → Microsoft.App/sandboxGroups (preview)
//     foundry.bicep        → Microsoft.CognitiveServices/accounts + project + deployment
//     monitoring.bicep     → Log Analytics + workspace-based App Insights
//
//   Net-new for this repo:
//     registry.bicep       → Microsoft.ContainerRegistry/registries (for FHA image)
//     rbac.bicep           → Role assignments (caller + project MSI)
// =============================================================================

targetScope = 'resourceGroup'

param location string
param foundryLocation string = location
param resourceToken string
param tags object

param foundryModelName string
param foundryModelVersion string
param foundryModelSkuName string
param foundryModelCapacity int

param principalId string
param principalType string

// --- Naming ---
var sandboxGroupName    = 'sg-fha-${resourceToken}'
var foundryAccountName  = 'aif-fha-${resourceToken}'
var foundryProjectName  = 'proj-fha-codeact'
var acrName             = take('crfha${resourceToken}', 50)
var logAnalyticsName    = 'log-fha-${resourceToken}'
var appInsightsName     = 'appi-fha-${resourceToken}'

// --- Modules ---

module sandboxGroup 'modules/sandboxgroup.bicep' = {
  name: 'sandboxGroup'
  params: {
    location: location
    tags: tags
    sandboxGroupName: sandboxGroupName
  }
}

module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  params: {
    location: foundryLocation
    tags: tags
    accountName: foundryAccountName
    projectName: foundryProjectName
    modelName: foundryModelName
    modelVersion: foundryModelVersion
    modelSkuName: foundryModelSkuName
    modelCapacity: foundryModelCapacity
    // Linking App Insights as a project connection is what turns on
    // platform-managed telemetry injection into the FHA agent container.
    // See foundry.bicep's appInsightsConnection resource for details.
    appInsightsId: monitoring.outputs.appInsightsResourceId
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  params: {
    location: location
    tags: tags
    registryName: acrName
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    tags: tags
    logAnalyticsName: logAnalyticsName
    appInsightsName: appInsightsName
  }
}

// Role assignments: must run after the principal-bearing resources exist.
module rbac 'modules/rbac.bicep' = {
  name: 'rbac'
  params: {
    sandboxGroupName: sandboxGroup.outputs.name
    foundryAccountName: foundry.outputs.accountName
    registryName: registry.outputs.name
    logAnalyticsName: monitoring.outputs.logAnalyticsName
    callerPrincipalId: principalId
    callerPrincipalType: principalType
    foundryProjectPrincipalId: foundry.outputs.projectPrincipalId
  }
}

// --- Outputs ---
output sandboxGroupName string = sandboxGroup.outputs.name
output foundryAccountName string = foundry.outputs.accountName
output foundryAccountId string = foundry.outputs.accountId
output foundryProjectName string = foundry.outputs.projectName
output foundryProjectId string = foundry.outputs.projectId
output foundryProjectEndpoint string = foundry.outputs.projectEndpoint
output foundryModelDeploymentName string = foundry.outputs.modelDeploymentName
output acrName string = registry.outputs.name
output acrLoginServer string = registry.outputs.loginServer
output appInsightsConnectionString string = monitoring.outputs.appInsightsConnectionString
output appInsightsName string = monitoring.outputs.appInsightsName
output logAnalyticsName string = monitoring.outputs.logAnalyticsName
output logAnalyticsResourceId string = monitoring.outputs.logAnalyticsResourceId
output logAnalyticsCustomerId string = monitoring.outputs.logAnalyticsCustomerId
