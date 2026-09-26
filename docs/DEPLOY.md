# Deployment

Ordering matters. Nothing can deploy into resources that don't exist yet, so provision infrastructure first.

```
1. OIDC          -> app registration + federated credentials
2. Infra         -> bicep creates ACA env, SWA, ACR, Storage, logs
3. Foundry       -> deploy models, grant RBAC
4. Secrets       -> from infra outputs
5. App deploys   -> backend + frontend workflows
```

Until step 4 is done, the `deploy` jobs **skip with an explanation** rather than failing. `test`, `lint`, `build`, and the bicep `validate` job all run without any Azure access.

---

## 1. GitHub OIDC

No stored passwords — GitHub exchanges a short-lived token.

```bash
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
REPO="zzurlo/agent-harness"

APP_ID=$(az ad app create --display-name agent-harness-ci --query appId -o tsv)
az ad sp create --id "$APP_ID"

# Federated credentials: one per trigger context.
for SUBJECT in "repo:${REPO}:ref:refs/heads/main" "repo:${REPO}:pull_request"; do
  NAME=$(echo "$SUBJECT" | tr ':/' '--')
  az ad app federated-credential create --id "$APP_ID" --parameters "{
    \"name\": \"${NAME}\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"${SUBJECT}\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }"
done

az group create -n rg-agent-harness -l centralus

# Contributor on the RG only -- not the subscription.
az role assignment create \
  --assignee "$APP_ID" \
  --role Contributor \
  --scope "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/rg-agent-harness"

# User Access Administrator is required because the template creates an
# AcrPull role assignment. Scoped to the RG.
az role assignment create \
  --assignee "$APP_ID" \
  --role "User Access Administrator" \
  --scope "/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/rg-agent-harness"

echo "AZURE_CLIENT_ID=$APP_ID"
echo "AZURE_TENANT_ID=$TENANT_ID"
echo "AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION_ID"
```

Set those three as repo secrets:

```bash
gh secret set AZURE_CLIENT_ID --body "$APP_ID"
gh secret set AZURE_TENANT_ID --body "$TENANT_ID"
gh secret set AZURE_SUBSCRIPTION_ID --body "$SUBSCRIPTION_ID"
```

## 2. Foundry project

Create the project and deploy models — five routes, but start with two if you want to keep it simple (`chat` and `tools`).

```bash
az cognitiveservices account create \
  -n foundry-agentharness -g rg-agent-harness \
  -l centralus --kind AIServices --sku S0 --yes

az cognitiveservices account show \
  -n foundry-agentharness -g rg-agent-harness \
  --query properties.endpoint -o tsv
```

Deploy models in the [Foundry portal](https://ai.azure.com) under **Models + Endpoints**. For each one, confirm two things in the catalog before wiring it up:

- current **pricing in your region** (serverless rates vary by region and change)
- whether it actually supports **function calling** — reasoning-tagged does not imply tool-capable

Then:

```bash
gh secret set FOUNDRY_PROJECT_ENDPOINT \
  --body "https://<resource>.services.ai.azure.com/api/projects/<project>"
```

### Verify the deployments before you deploy the app

```bash
cd backend
export FOUNDRY_PROJECT_ENDPOINT="https://<res>.services.ai.azure.com/api/projects/<proj>"
python scripts/verify_models.py
```

```
Foundry model verification
endpoint: https://foundry-agentharness.services.ai.azure.com/api/projects/harness
auth:     Entra ID (DefaultAzureCredential)

  PASS  fast    Phi-4-mini-reasoning                  340ms
  PASS  chat    Llama-3.3-70B-Instruct                810ms  tools ok
  FAIL  tools   grok-4.1-fast-reasoning               520ms  tools broken
        -> accepted the tool schema but emitted no tool call -- this model
           will silently never use tools.
  FAIL  think   DeepSeek-V4-Flash                         -
        -> deployment not found -- check the name in Foundry portal

2 of 4 routes unusable.
```

Each route gets two probes: a trivial completion, and — for routes flagged `supports_tools` — a tool-calling probe that can only be answered by emitting a call.

The second probe is the one that matters. A model that **accepts your tool schema and then ignores it** produces no error anywhere; your agent loop just quietly never calls a tool, and you find out mid-conversation. This catches it before deploy.

| Flag | Effect |
|---|---|
| `--route tools` | check one route (repeatable) |
| `--json` | machine-readable output |
| `--skip-tools` | completion probe only |
| `--timeout 90` | per-request timeout |

Exit codes: `0` all usable, `1` at least one hard failure, `2` config/credential problem.

The backend deploy workflow runs this automatically after each deploy (non-blocking) and writes the results table to the job summary.

## 3. Provision infrastructure

Actions → **Deploy infrastructure** → Run workflow. Or locally:

```bash
az deployment group create \
  -g rg-agent-harness \
  -f infra/main.bicep \
  -p foundryProjectEndpoint="https://<res>.services.ai.azure.com/api/projects/<proj>"
```

> **First deploy uses a public placeholder image.** This is deliberate. The container app can't pull from ACR until its managed identity has the AcrPull role, but that role needs a principalId that only exists after the app is created — and the image isn't in ACR yet anyway. Deploy once with the placeholder; the backend workflow pushes the real image. Pass `-p useAcrImage=true` on later runs.

The workflow prints the outputs you need in its job summary.

## 4. Remaining secrets

```bash
API_URL=$(az containerapp show -n ca-agentharness-api -g rg-agent-harness \
  --query properties.configuration.ingress.fqdn -o tsv)
ACR_NAME=$(az acr list -g rg-agent-harness --query "[0].name" -o tsv)
SWA_TOKEN=$(az staticwebapp secrets list -n swa-agentharness \
  -g rg-agent-harness --query properties.apiKey -o tsv)
SWA_HOST=$(az staticwebapp show -n swa-agentharness -g rg-agent-harness \
  --query defaultHostname -o tsv)

gh secret set ACR_NAME --body "$ACR_NAME"
gh secret set VITE_API_BASE --body "https://$API_URL"
gh secret set AZURE_STATIC_WEB_APPS_API_TOKEN --body "$SWA_TOKEN"
```

Lock CORS down to the real frontend origin (the template defaults to `*`):

```bash
az deployment group create -g rg-agent-harness -f infra/main.bicep \
  -p corsOrigins="https://$SWA_HOST" \
     foundryProjectEndpoint="<...>"
```

## 5. Grant the API access to Foundry

The container app authenticates to Foundry with its managed identity — no keys in config.

```bash
PRINCIPAL_ID=$(az containerapp show -n ca-agentharness-api -g rg-agent-harness \
  --query identity.principalId -o tsv)
FOUNDRY_ID=$(az cognitiveservices account show -n foundry-agentharness \
  -g rg-agent-harness --query id -o tsv)

az role assignment create \
  --assignee "$PRINCIPAL_ID" \
  --role "Cognitive Services User" \
  --scope "$FOUNDRY_ID"
```

> Azure AI RBAC roles were renamed: **Azure AI User → Foundry User**, Azure AI Owner → Foundry Owner, etc. Role IDs and permissions are unchanged, and you may still see old names in the portal during rollout.

## 6. Deploy the app

```bash
git push origin main
```

Backend builds in ACR, updates the container app, and health-checks with cold-start retries. Frontend builds and uploads to SWA.

---

## Secrets reference

| Secret | Source | Needed for |
|---|---|---|
| `AZURE_CLIENT_ID` | step 1 | all Azure jobs |
| `AZURE_TENANT_ID` | step 1 | all Azure jobs |
| `AZURE_SUBSCRIPTION_ID` | step 1 | all Azure jobs |
| `FOUNDRY_PROJECT_ENDPOINT` | step 2 | infra deploy |
| `ACR_NAME` | step 4 | backend deploy |
| `VITE_API_BASE` | step 4 | frontend build |
| `AZURE_STATIC_WEB_APPS_API_TOKEN` | step 4 | frontend deploy |

## Troubleshooting

**`Login failed ... Ensure 'client-id' and 'tenant-id' are supplied`**
OIDC secrets missing. Step 1.

**`deployment_token was not provided`**
`AZURE_STATIC_WEB_APPS_API_TOKEN` missing. Step 4. The build still succeeds and uploads an artifact.

**`Container app 'ca-agentharness-api' not found`**
Infra not provisioned. Step 3.

**Container app won't start / `ImagePullFailure`**
Redeployed with `useAcrImage=true` before an image existed in ACR. Run the backend workflow first.

**First request takes 10 seconds**
Expected — `minReplicas: 0`. The frontend's warmup ping on page load hides this in normal use.

**`401` from Foundry**
Managed identity lacks the role, or the endpoint is wrong. Verify:
```bash
az containerapp show -n ca-agentharness-api -g rg-agent-harness \
  --query "properties.template.containers[0].env" -o table
```

**Costs higher than expected**
Check that `minReplicas` is still 0 and the Log Analytics daily cap is intact:
```bash
az containerapp show -n ca-agentharness-api -g rg-agent-harness \
  --query properties.template.scale -o json
az monitor log-analytics workspace show -n log-agentharness \
  -g rg-agent-harness --query workspaceCapping -o json
```
