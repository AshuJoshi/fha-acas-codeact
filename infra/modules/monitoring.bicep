// Log Analytics workspace + workspace-based App Insights.
//
// Workspace-based AI is the only flavor supported for new deployments;
// the legacy classic AI account is retired. The agent container reads
// ``APPLICATIONINSIGHTS_CONNECTION_STRING`` from its environment if set
// (Agent Framework wires OTel export automatically).
//
// Vendored verbatim from acas-toolkit/infra/modules/monitoring.bicep.

param location string
param tags object
param logAnalyticsName string
param appInsightsName string

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource ai 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

output logAnalyticsName string = law.name
output logAnalyticsResourceId string = law.id
output logAnalyticsCustomerId string = law.properties.customerId
output appInsightsName string = ai.name
output appInsightsResourceId string = ai.id
output appInsightsConnectionString string = ai.properties.ConnectionString
output appInsightsInstrumentationKey string = ai.properties.InstrumentationKey
