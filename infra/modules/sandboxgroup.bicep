// Azure Container Apps Sandboxes: sandbox group (GA API ``2026-07-01``).
//
// The GA schema dropped ``allowedLocations`` and no longer documents
// ``managementEndpoint``; the toolkit resolves the endpoint via the SDK at runtime.
//
// Adapted from acas-toolkit/infra/modules/sandboxgroup.bicep.

param location string
param tags object
param sandboxGroupName string

resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-07-01' = {
  name: sandboxGroupName
  location: location
  tags: tags
  // GA preflight rejects a missing properties object, even though no property is required.
  properties: {}
}

output name string = sandboxGroup.name
output id string = sandboxGroup.id
