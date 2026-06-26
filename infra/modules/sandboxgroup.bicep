// Azure Container Apps Sandboxes — sandbox group.
//
// Preview resource (``Microsoft.App/sandboxGroups``, API
// ``2026-02-01-preview``). The sandbox group is the regional control
// plane the ACAS SDK targets; ``properties.managementEndpoint`` is the
// regional URL the toolkit's data client routes through.
//
// SKU is intentionally null (the preview supports no SKU choice).
// ``allowedLocations`` pins the data-plane to a single region for
// predictable testing.
//
// Vendored verbatim from acas-toolkit/infra/modules/sandboxgroup.bicep.

param location string
param tags object
param sandboxGroupName string

resource sandboxGroup 'Microsoft.App/sandboxGroups@2026-02-01-preview' = {
  name: sandboxGroupName
  location: location
  tags: tags
  properties: {
    allowedLocations: [
      location
    ]
  }
}

output name string = sandboxGroup.name
output id string = sandboxGroup.id
// managementEndpoint is populated asynchronously after create; consumers
// (the agent and orchestrator) re-resolve it via ARM at runtime. Exposed
// here for debugging via ``azd env get-values``.
output managementEndpoint string = sandboxGroup.properties.managementEndpoint
