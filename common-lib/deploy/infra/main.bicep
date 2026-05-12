targetScope = 'resourceGroup'

@description('Deployment location')
param location string = resourceGroup().location

@description('Normalized school token. Keep this within the naming budget for KV and Storage.')
@minLength(2)
@maxLength(7)
param schoolNameToken string = 'school'

@allowed([
  'es'
  'ms'
  'hs'
  'sc'
  'kg'
])
@description('School level token')
param schoolLevel string = 'es'

@allowed([
  'test'
  'prod'
])
@description('Deployment environment suffix')
param envSuffix string = 'prod'

@maxLength(2)
@description('Two-digit deployment sequence')
param deployNum string = '01'

@allowed([
  'krc'
  'cin'
  'eas'
  'jpe'
])
@description('Region short code used in resource names. Example: krc, cin, eas, jpe')
param regionCode string = 'krc'

@description('Enable Cosmos DB free tier. Only one free-tier account is allowed per subscription.')
param enableCosmosFreeTier bool = false

@description('If true, wire Key Vault secret references into Container App runtime settings. Run this only after required secrets exist in Key Vault.')
param configureRuntimeSecrets bool = false

@description('Container listening port')
param containerPort int = 80

@description('Liveness and readiness probe path')
param probePath string = '/'

@description('Minimum replicas for the Container App')
param minReplicas int = 0

@description('Maximum replicas for the Container App')
param maxReplicas int = 2

@description('Enable inbound IP restrictions for Container App ingress. Keep false in development.')
param enableIpRestriction bool = false

@description('Allowed inbound CIDR ranges when IP restriction is enabled, for example [\'203.0.113.0/24\'].')
param allowedCidrs array = []

@description('Allowed browser origins for CORS, for example [\'https://school.example.kr\']. Keep empty to disable CORS policy.')
param corsAllowedOrigins array = []

@description('Name of an existing Container App Environment to reuse. Leave empty to create a new one. Only one Managed Environment is allowed per region per subscription.')
param existingContainerAppEnvName string = ''

@description('Resource group of the existing Container App Environment. Required when existingContainerAppEnvName is set and the environment is in a different resource group.')
param existingContainerAppEnvResourceGroup string = ''

@description('Name of an existing shared Cosmos DB account to reuse. Leave empty to create a new one. Only one free-tier Cosmos DB is allowed per subscription.')
param existingCosmosDbName string = ''

@description('Resource group of the existing shared Cosmos DB account. Used for cross-resource-group reference when existingCosmosDbName is set.')
param existingCosmosDbResourceGroup string = ''

var kvName = 'kv-${schoolNameToken}-${schoolLevel}-${envSuffix}-${regionCode}${deployNum}'
var storageName = 'st${schoolNameToken}${schoolLevel}${envSuffix}${regionCode}${deployNum}'
var cosmosName = !empty(existingCosmosDbName) ? existingCosmosDbName : 'cosmos-${schoolNameToken}-${schoolLevel}-${envSuffix}-${regionCode}${deployNum}'
var acrName = 'acr${schoolNameToken}${schoolLevel}${envSuffix}${regionCode}${deployNum}'
var logWorkspaceName = 'law-${schoolNameToken}-${schoolLevel}-${envSuffix}-${regionCode}${deployNum}'
var envName = 'env-${schoolNameToken}-${schoolLevel}-${envSuffix}-${regionCode}${deployNum}'
var appName = 'app-${schoolNameToken}-${schoolLevel}-${envSuffix}-${regionCode}${deployNum}'
var identityName = 'id-${schoolNameToken}-${schoolLevel}-${envSuffix}-${regionCode}${deployNum}'

var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var storageBlobRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
var keyVaultSecretsUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')

var manualSecretNames = [
  'api-key-ai'
  'azure-openai-endpoint'
  'api-key-sms'
  'api-secret-sms'
  'sms-sender-phone'
  'encryption-key-aes'
  'encryption-key-hmac'
]

var autoSecretNames = [
  'connection-string-storage'
  'connection-string-database'
]

var generatedIpSecurityRestrictions = [for (cidr, i) in allowedCidrs: {
  name: 'AllowCIDR${i}'
  ipAddressRange: cidr
  action: 'Allow'
}]
var ipSecurityRestrictions = enableIpRestriction ? generatedIpSecurityRestrictions : []
var ingressConfig = empty(corsAllowedOrigins) ? {
  external: true
  targetPort: containerPort
  ipSecurityRestrictions: ipSecurityRestrictions
} : {
  external: true
  targetPort: containerPort
  ipSecurityRestrictions: ipSecurityRestrictions
  corsPolicy: {
    allowedOrigins: corsAllowedOrigins
    allowedMethods: [
      'GET'
      'POST'
      'OPTIONS'
    ]
    allowedHeaders: [
      '*'
    ]
  }
}

var containerSecrets = configureRuntimeSecrets ? [
  {
    name: 'api-key-ai'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/api-key-ai'
    identity: appIdentity.id
  }
  {
    name: 'azure-openai-endpoint'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/azure-openai-endpoint'
    identity: appIdentity.id
  }
  {
    name: 'api-key-sms'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/api-key-sms'
    identity: appIdentity.id
  }
  {
    name: 'api-secret-sms'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/api-secret-sms'
    identity: appIdentity.id
  }
  {
    name: 'sms-sender-phone'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/sms-sender-phone'
    identity: appIdentity.id
  }
  {
    name: 'encryption-key-aes'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/encryption-key-aes'
    identity: appIdentity.id
  }
  {
    name: 'encryption-key-hmac'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/encryption-key-hmac'
    identity: appIdentity.id
  }
  {
    name: 'connection-string-storage'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/connection-string-storage'
    identity: appIdentity.id
  }
  {
    name: 'connection-string-database'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/connection-string-database'
    identity: appIdentity.id
  }
] : []

var containerEnv = configureRuntimeSecrets ? [
  {
    name: 'ENCRYPTION_KEY_AES'
    secretRef: 'encryption-key-aes'
  }
  {
    name: 'ENCRYPTION_KEY_HMAC'
    secretRef: 'encryption-key-hmac'
  }
  {
    name: 'DATABASE_CONNECTION_STRING'
    secretRef: 'connection-string-database'
  }
  {
    name: 'STORAGE_CONNECTION_STRING'
    secretRef: 'connection-string-storage'
  }
  {
    name: 'AI_SERVICE_KEY'
    secretRef: 'api-key-ai'
  }
  {
    name: 'AZURE_OPENAI_ENDPOINT'
    secretRef: 'azure-openai-endpoint'
  }
  {
    name: 'SMS_API_KEY'
    secretRef: 'api-key-sms'
  }
  {
    name: 'SMS_API_SECRET'
    secretRef: 'api-secret-sms'
  }
  {
    name: 'SMS_SENDER_PHONE'
    secretRef: 'sms-sender-phone'
  }
  {
    name: 'COSMOS_DATABASE'
    value: 'CertificateSystem'
  }
  {
    name: 'COSMOS_CONTAINER'
    value: 'records'
  }
  {
    name: 'COSMOS_ADMINS_CONTAINER'
    value: 'admins'
  }
  {
    name: 'COSMOS_REGISTRY_CONTAINER'
    value: 'registry'
  }
  {
    name: 'STORAGE_CONTAINER'
    value: 'record-files'
  }
  {
    name: 'WEBSITES_INCLUDE_CLOUD_CERTS'
    value: 'true'
  }
] : []

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logWorkspaceName
  location: location
  properties: {
    retentionInDays: 30
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-02-01' = {
  name: kvName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
  }
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 30
    }
  }
}

resource webpsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'webps'
}

resource recordFilesContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'record-files'
}

resource storageLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-01-01' = {
  parent: storageAccount
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          enabled: true
          name: 'archive-policy'
          type: 'Lifecycle'
          definition: {
            actions: {
              baseBlob: {
                tierToCool: {
                  daysAfterModificationGreaterThan: 1095
                }
                delete: {
                  daysAfterModificationGreaterThan: 3285
                }
              }
            }
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                'webps/'
              ]
            }
          }
        }
      ]
    }
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts@2023-04-15' = if (empty(existingCosmosDbName)) {
  name: cosmosName
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    enableFreeTier: enableCosmosFreeTier
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
      }
    ]
  }
}

var cosmosDbName = !empty(existingCosmosDbName) ? existingCosmosDbName : cosmosName

resource cosmosDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2023-04-15' = if (empty(existingCosmosDbName)) {
  name: '${cosmosDbName}/CertificateSystem'
  properties: {
    resource: {
      id: 'CertificateSystem'
    }
    options: {
      throughput: 400
    }
  }
}

resource registryContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2023-04-15' = if (empty(existingCosmosDbName)) {
  name: '${cosmosDbName}/CertificateSystem/registry'
  properties: {
    resource: {
      id: 'registry'
      partitionKey: {
        paths: [
          '/cohortId'
        ]
        kind: 'Hash'
      }
    }
  }
}

resource recordsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2023-04-15' = if (empty(existingCosmosDbName)) {
  name: '${cosmosDbName}/CertificateSystem/records'
  properties: {
    resource: {
      id: 'records'
      partitionKey: {
        paths: [
          '/studentId'
        ]
        kind: 'Hash'
      }
    }
  }
}

resource adminsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2023-04-15' = if (empty(existingCosmosDbName)) {
  name: '${cosmosDbName}/CertificateSystem/admins'
  properties: {
    resource: {
      id: 'admins'
      partitionKey: {
        paths: [
          '/email'
        ]
        kind: 'Hash'
      }
    }
  }
}

resource aiLimitsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2023-04-15' = if (empty(existingCosmosDbName)) {
  name: '${cosmosDbName}/CertificateSystem/ai_limits'
  properties: {
    resource: {
      id: 'ai_limits'
      partitionKey: {
        paths: [
          '/id'
        ]
        kind: 'Hash'
      }
      defaultTtl: -1
    }
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-01-01-preview' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, appIdentity.id, acrPullRoleId)
  properties: {
    principalId: appIdentity.properties.principalId
    roleDefinitionId: acrPullRoleId
    principalType: 'ServicePrincipal'
  }
}

resource storageBlobAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name: guid(storageAccount.id, appIdentity.id, storageBlobRoleId)
  properties: {
    principalId: appIdentity.properties.principalId
    roleDefinitionId: storageBlobRoleId
    principalType: 'ServicePrincipal'
  }
}

resource keyVaultSecretsAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, appIdentity.id, keyVaultSecretsUserRoleId)
  properties: {
    principalId: appIdentity.properties.principalId
    roleDefinitionId: keyVaultSecretsUserRoleId
    principalType: 'ServicePrincipal'
  }
}

resource storageConnectionSecret 'Microsoft.KeyVault/vaults/secrets@2023-02-01' = {
  parent: keyVault
  name: 'connection-string-storage'
  properties: {
    value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};AccountKey=${storageAccount.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
  }
}

resource cosmosConnectionSecret 'Microsoft.KeyVault/vaults/secrets@2023-02-01' = if (empty(existingCosmosDbName)) {
  parent: keyVault
  name: 'connection-string-database'
  properties: {
    value: cosmosDb.listConnectionStrings().connectionStrings[0].connectionString
  }
}

resource containerAppEnv 'Microsoft.App/managedEnvironments@2023-05-01' = if (empty(existingContainerAppEnvName)) {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  dependsOn: [
    acrPullAssignment
    storageBlobAssignment
    keyVaultSecretsAssignment
    storageConnectionSecret
    cosmosConnectionSecret
  ]
  properties: {
    managedEnvironmentId: !empty(existingContainerAppEnvName) ? resourceId(!empty(existingContainerAppEnvResourceGroup) ? existingContainerAppEnvResourceGroup : resourceGroup().name, 'Microsoft.App/managedEnvironments', existingContainerAppEnvName) : containerAppEnv.id
    configuration: {
      ingress: ingressConfig
      registries: [
        {
          server: '${acr.name}.azurecr.io'
          identity: appIdentity.id
        }
      ]
      secrets: containerSecrets
    }
    template: {
      containers: [
        {
          name: appName
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          env: containerEnv
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: probePath
                port: containerPort
              }
              initialDelaySeconds: 30
              periodSeconds: 10
            }
            {
              type: 'Readiness'
              httpGet: {
                path: probePath
                port: containerPort
              }
              initialDelaySeconds: 5
              periodSeconds: 5
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-rule'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
output containerAppName string = containerApp.name
output containerEnvironmentName string = !empty(existingContainerAppEnvName) ? existingContainerAppEnvName : containerAppEnv.name
output containerRegistryName string = acr.name
output userAssignedIdentityResourceId string = appIdentity.id
output requiredManualSecretNames array = manualSecretNames
output autoManagedSecretNames array = autoSecretNames
