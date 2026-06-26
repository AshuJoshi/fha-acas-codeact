// Azure Container Registry for the FHA agent image.
//
// `azd deploy` performs a remote build inside the registry (driven by
// `docker.remoteBuild: true` in azure.yaml), so the local dev machine
// does NOT need Docker. The Foundry project then pulls the image to
// register a new agent version.
//
// `Basic` SKU is sufficient for a single agent. AAD-only auth
// (``adminUserEnabled: false``) — both ``azd deploy`` (as the caller)
// and Foundry (as the project MSI) get AcrPull/AcrPush via rbac.bicep.

param location string
param tags object

@minLength(5)
@maxLength(50)
@description('ACR name. Must be globally unique, 5-50 alphanumeric chars.')
param registryName string

@allowed([ 'Basic', 'Standard', 'Premium' ])
param sku string = 'Basic'

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: sku
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    anonymousPullEnabled: false
  }
}

output name string = registry.name
output id string = registry.id
output loginServer string = registry.properties.loginServer
