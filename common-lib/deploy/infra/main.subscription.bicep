targetScope = 'subscription'

@description('Deployment location for both resource group and resources.')
param location string = 'koreacentral'

@description('Normalized school token. Keep this within the naming budget for KV and Storage.')
param schoolNameToken string = 'kuhwa'

@allowed([
  'es'
  'ms'
  'hs'
  'sc'
  'kg'
])
@description('School level token')
param schoolLevel string = 'sc'

@allowed([
  'test'
  'prod'
])
@description('Deployment environment suffix')
param envSuffix string = 'test'

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

@description('Optional override for resource group name. Leave empty to auto-generate.')
param resourceGroupName string = ''

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

var rgName = !empty(resourceGroupName) ? resourceGroupName : 'rg-${schoolNameToken}-${schoolLevel}-${envSuffix}-${regionCode}${deployNum}'

resource resourceGroup 'Microsoft.Resources/resourceGroups@2022-09-01' = {
  name: rgName
  location: location
  tags: {
    workload: 'schooldocs'
    schoolNameToken: schoolNameToken
    schoolLevel: schoolLevel
    envSuffix: envSuffix
    regionCode: regionCode
  }
}

module mainDeployment 'main.bicep' = {
  name: 'schooldocs-main-${uniqueString(subscription().id, rgName)}'
  scope: resourceGroup
  params: {
    location: location
    schoolNameToken: schoolNameToken
    schoolLevel: schoolLevel
    envSuffix: envSuffix
    deployNum: deployNum
    regionCode: regionCode
    enableCosmosFreeTier: enableCosmosFreeTier
    configureRuntimeSecrets: configureRuntimeSecrets
    containerPort: containerPort
    probePath: probePath
    minReplicas: minReplicas
    maxReplicas: maxReplicas
    enableIpRestriction: enableIpRestriction
    allowedCidrs: allowedCidrs
    corsAllowedOrigins: corsAllowedOrigins
    existingContainerAppEnvName: existingContainerAppEnvName
    existingContainerAppEnvResourceGroup: existingContainerAppEnvResourceGroup
    existingCosmosDbName: existingCosmosDbName
    existingCosmosDbResourceGroup: existingCosmosDbResourceGroup
  }
}

output resourceGroupName string = rgName
output keyVaultName string = mainDeployment.outputs.keyVaultName
output keyVaultUri string = mainDeployment.outputs.keyVaultUri
output containerAppName string = mainDeployment.outputs.containerAppName
output containerEnvironmentName string = mainDeployment.outputs.containerEnvironmentName
output containerRegistryName string = mainDeployment.outputs.containerRegistryName
output userAssignedIdentityResourceId string = mainDeployment.outputs.userAssignedIdentityResourceId
output requiredManualSecretNames array = mainDeployment.outputs.requiredManualSecretNames
output autoManagedSecretNames array = mainDeployment.outputs.autoManagedSecretNames
