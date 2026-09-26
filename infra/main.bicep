// Infrastructure for agent-harness.
//
// Cost posture: everything here is free-tier or scale-to-zero.
//   Static Web Apps  Free   -> $0
//   Container Apps   min=0  -> $0 idle, ~$0-5/mo active
//   Log Analytics    capped -> $0-3/mo
//   Storage (Tables)        -> pennies
// Deliberately NO Postgres: a B1ms Flexible Server would be ~$13/mo, more than
// every other line item combined.

targetScope = 'resourceGroup'

@description('Base name for all resources.')
param name string = 'agentharness'

@description('Location. Must support the Foundry Responses API.')
@allowed([
  'centralus'
  'eastus'
  'eastus2'
  'westus3'
  'swedencentral'
])
param location string = 'centralus'

@description('Container image for the backend.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Foundry project endpoint, e.g. https://<res>.services.ai.azure.com/api/projects/<proj>')
param foundryProjectEndpoint string = ''

@description('Allowed CORS origins (the SWA hostname).')
param corsOrigins string = ''

var uniq = uniqueString(resourceGroup().id)

// ---------------------------------------------------------------------------
// Observability -- capped so it cannot become the biggest line item
// ---------------------------------------------------------------------------
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${name}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: {
      dailyQuotaGb: json('0.5')
    }
  }
}

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: 'acr${name}${uniq}'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// ---------------------------------------------------------------------------
// Thread persistence -- Table Storage, not Postgres
// ---------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'st${name}${uniq}'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

// ---------------------------------------------------------------------------
// Container Apps -- its own environment, isolated from any other project
// ---------------------------------------------------------------------------
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'env-${name}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-${name}-api'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        corsPolicy: {
          allowedOrigins: empty(corsOrigins) ? ['*'] : split(corsOrigins, ',')
          allowedMethods: ['GET', 'POST', 'DELETE', 'OPTIONS']
          allowedHeaders: ['*']
        }
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: 'system'
        }
      ]
      secrets: [
        {
          name: 'tables-connection'
          value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
            { name: 'CORS_ORIGINS', value: corsOrigins }
            { name: 'TABLES_CONNECTION_STRING', secretRef: 'tables-connection' }
            { name: 'LOG_LEVEL', value: 'INFO' }
          ]
          probes: [
            {
              type: 'Readiness'
              httpGet: { path: '/health', port: 8000 }
              initialDelaySeconds: 3
              periodSeconds: 5
            }
          ]
        }
      ]
      scale: {
        // THE cost lever: zero replicas when idle.
        minReplicas: 0
        maxReplicas: 2
        rules: [
          {
            name: 'http-concurrency'
            http: { metadata: { concurrentRequests: '20' } }
          }
        ]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Frontend -- SWA Free tier.
// Note: the "linked backend" proxy needs Standard ($9/mo); we use CORS instead.
// ---------------------------------------------------------------------------
resource swa 'Microsoft.Web/staticSites@2023-12-01' = {
  name: 'swa-${name}'
  location: location
  sku: { name: 'Free', tier: 'Free' }
  properties: {
    buildProperties: {
      appLocation: '/frontend'
      outputLocation: 'dist'
    }
  }
}

// Let the container app pull from ACR using its managed identity.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, api.id, 'AcrPull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: api.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output apiUrl string = 'https://${api.properties.configuration.ingress.fqdn}'
output acrLoginServer string = acr.properties.loginServer
output swaHostname string = swa.properties.defaultHostname
output apiPrincipalId string = api.identity.principalId
