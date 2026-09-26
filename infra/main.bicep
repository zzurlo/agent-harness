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

@description('''
Container image for the backend.

Leave as the default placeholder on FIRST deploy. There is an ordering problem
otherwise: the container app cannot pull from ACR until its managed identity
has the AcrPull role, but that role assignment needs the identity's principalId,
which only exists after the app is created. And on a brand-new registry the
image has not been pushed yet regardless.

So: deploy once with the public placeholder, then let the backend workflow
build and push the real image via `az containerapp update`.
''')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('True once a real image exists in ACR. Enables the ACR registry binding.')
param useAcrImage bool = false

@description('Server-only bearer token. Required for a real image; scripts/deploy.py validates strength.')
@secure()
param appAccessToken string = ''

@description('Live environment, secrets and registries preserved by scripts/deploy.py. Never supply through CLI argv.')
@secure()
param runtimeConfig object

// Use scripts/deploy.py, not raw defaults, for every routine infrastructure rerun.
var defaultEnv = {
  CORS_ORIGINS: { name: 'CORS_ORIGINS', value: 'https://${swa.properties.defaultHostname}' }
  LOG_LEVEL: { name: 'LOG_LEVEL', value: 'INFO' }
  TABLES_CONNECTION_STRING: { name: 'TABLES_CONNECTION_STRING', secretRef: 'tables-connection' }
}
var runtimeEnv = union(defaultEnv, toObject(runtimeConfig.env, entry => entry.name, entry => entry), useAcrImage ? {
  APP_ACCESS_TOKEN: { name: 'APP_ACCESS_TOKEN', secretRef: 'app-access-token' }
} : {})

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
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: useAcrImage ? 8000 : 80
        transport: 'auto'
        allowInsecure: false
      }
      // Bootstrap identity must exist before AcrPull can be assigned.
      registries: union(runtimeConfig.registries, useAcrImage ? [
        {
          server: acr.properties.loginServer
          identity: 'system'
        }
      ] : [])
      secrets: concat(runtimeConfig.secrets, [
        {
          name: 'tables-connection'
          value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
        }
      ], useAcrImage ? [{ name: 'app-access-token', value: appAccessToken }] : [])
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
          env: map(items(runtimeEnv), entry => entry.value)
          probes: [
            {
              type: 'Readiness'
              httpGet: { path: useAcrImage ? '/health' : '/', port: useAcrImage ? 8000 : 80 }
              initialDelaySeconds: 3
              periodSeconds: 5
            }
          ]
        }
      ]
      scale: {
        // THE cost lever: zero replicas when idle.
        minReplicas: 0
        maxReplicas: 1
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
