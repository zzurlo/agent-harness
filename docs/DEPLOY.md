# Deployment runbook

All commands target **zzurlo/agent-harness** and **rg-agent-harness**, not FieldForge. Use an authenticated Azure Cloud Shell Bash or local Bash with Azure CLI/GitHub CLI. Set/check the intended subscription first. Commands below change Azure only when explicitly described.

**Merging this PR deploys nothing.** PRs/main pushes validate code; provisioning and both app uploads require a manual workflow dispatch on `main`. Infrastructure dispatch defaults to read-only what-if.

## 1. Deployment identity (already completed for this project)

GitHub Actions uses a dedicated Entra application/service principal with these repository **secrets**:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`

OIDC federated credential:

```text
issuer:   https://token.actions.githubusercontent.com
subject:  repo:zzurlo/agent-harness:ref:refs/heads/main
audience: api://AzureADTokenExchange
```

The identity needs Contributor and role-assignment permission (currently User Access Administrator), scoped **only to rg-agent-harness**. The latter creates the API identity's AcrPull grant. Do not grant privileged pull-request trust to make previews green. Built-in PR previews are intentionally absent.

Resource-provider registration is subscription-scoped. If this subscription has never used these services, a subscription administrator must register `Microsoft.App`, `Microsoft.ContainerRegistry`, `Microsoft.Storage`, `Microsoft.OperationalInsights`, and `Microsoft.Web` (plus `Microsoft.CognitiveServices` for Foundry). The RG-scoped CI identity cannot repair missing subscription registrations. Review Azure errors/Activity Log rather than granting subscription-wide Contributor to CI.

## 2. Inspect, then provision the isolated infrastructure

After merging, GitHub → Actions → **Deploy infrastructure** → **Run workflow**:

1. Branch **main**, action **what-if**, location **centralus**.
2. Inspect the proposed resource IDs/types and successful validation. What-if does not create an absent resource group. This project's resource group was created during identity setup.
3. Run again with action **deploy** only when ready to start resource charges.

Equivalent CLI:

```bash
gh workflow run infra.yml -R zzurlo/agent-harness --ref main \
  -f action=what-if -f location=centralus
# Separate, deliberate provisioning operation:
gh workflow run infra.yml -R zzurlo/agent-harness --ref main \
  -f action=deploy -f location=centralus
```

This creates SWA Free, ACA environment/app, ACR Basic, Storage and Log Analytics. It **does not create Foundry, model deployments, or inference RBAC**. The placeholder app uses port 80 and `/`; it is not the protected chat backend and must never be mistaken for a working app. Real backend promotion changes ingress/readiness to 8000 and `/health`.

The deployment helper prints only these non-secret outputs in its logs: `apiUrl`, `acrLoginServer`, `swaHostname`, `apiPrincipalId`.

**Do not deploy main.bicep directly with bootstrap defaults for a rerun.** `scripts/deploy.py` reads the current app and preserves its real image, model environment, secrets, and registry configuration. Authorization/network errors stop the operation instead of being treated as an empty environment. Secrets are held in memory/0600 temporary files, not printed; temporary files are deleted. A deployment may leave resources behind if it fails: inspect before retrying.

Infrastructure **deploy** reruns preserving a real application image also wait for the new revision and run the same mandatory health, anonymous auth-denial, and authenticated runtime model verification as backend deployments. The verifier receives the exact token submitted to ARM (the override, or the preserved live token), not a possibly empty/stale environment value. These checks make paid inference calls; failure or timeout fails the deployment command/workflow without rollback. Bootstrap placeholder deployments and all **what-if** runs never invoke this probe.

## 3. Set public configuration and private keys

GitHub repository → Settings → Secrets and variables → Actions.

Under **Variables**, set:

| Variable | Value |
|---|---|
| `ACR_NAME` | Registry name only, removing `.azurecr.io` from the registry output |
| `VITE_API_BASE` | Exact `apiUrl`, e.g. `https://ca-...azurecontainerapps.io` (no trailing path) |
| `FOUNDRY_MODEL_ENDPOINT` | Direct HTTPS resource `/openai/v1` endpoint, from step 4 |
| `MODEL_FAST` / `MODEL_CHAT` / `MODEL_TOOLS` / `MODEL_THINK` / `MODEL_MAX` | Actual deployed model names, not catalog labels |
| `CORS_ORIGINS` (optional) | Explicit comma-separated frontend origins; defaults to the provisioned SWA HTTPS origin |

`ACR_NAME` and `VITE_API_BASE` are now **variables**, not secrets. Do not copy old runbook instructions that put them in secrets. Empty/unset runtime variables on reruns preserve existing values, not erase them. To deliberately clear a setting, make an explicit reviewed configuration change.

Under **Secrets**, also set:

- `APP_ACCESS_TOKEN`: cryptographically random owner key, at least 32 non-whitespace characters. Generate locally with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`, store in your password manager, then paste directly into GitHub. Do not send it in chat or commit it. The same key unlocks the browser; it is never embedded in frontend assets.
- `AZURE_STATIC_WEB_APPS_API_TOKEN`: obtain from Azure's SWA deployment-token interface, or transfer straight to GitHub without printing:

```bash
set -o pipefail
az staticwebapp secrets list -n swa-agentharness -g rg-agent-harness \
  --query properties.apiKey -o tsv \
  | gh secret set AZURE_STATIC_WEB_APPS_API_TOKEN -R zzurlo/agent-harness
```

Health/warmup remain public. The real backend rejects all other unauthenticated requests. It fails closed with 503 if the owner key is absent/too short. This protects one shared owner workspace, not multiple users.

## 4. Configure Foundry models and the runtime identity

Create/select the intended Foundry resource and deploy compatible models in a supported region. Copy the deployment's direct model endpoint:

```text
https://<resource>.openai.azure.com/openai/v1
```

Do **not** use `https://...services.ai.azure.com/api/projects/<project>` or append `/v1` to it. Set `FOUNDRY_MODEL_ENDPOINT` and the five `MODEL_*` variables. A model may back multiple tiers, but capability flags in `backend/harness/config.py` must match it: chat/tools/max currently advertise tools; keep chat non-reasoning and validate actual function calls. No model choice or price in code proves regional availability.

Grant the **Container App system-assigned managed identity** (the `apiPrincipalId` output) the model-inference role required by the selected Foundry endpoint, scoped to the relevant resource/project per its current documentation. Verify the scope includes the actual model deployment. Role labels are changing (Azure AI User → Foundry User); do not substitute broad subscription permissions if a call fails. Record the chosen role/scope and validate with the runtime probe below.

The CI identity only deploys resources. It is **not** the identity used by chat or the runtime model probe. A successful model call from Cloud Shell/CI does not prove the API can call it.

Production uses asynchronous refreshing Entra credentials; no `FOUNDRY_API_KEY` is required. See [current SDK/endpoint documentation](https://learn.microsoft.com/en-us/azure/ai-foundry/how-to/develop/sdk-overview?view=foundry).

## 5. Deploy and verify the backend

Actions → **Deploy backend** → Run workflow → branch **main**, action **deploy**:

```bash
gh workflow run backend.yml -R zzurlo/agent-harness --ref main -f action=deploy
```

The workflow builds a SHA-tagged image in ACR, binds the system identity for registry pulls, updates image/port/probes together, and waits for the intended new revision. Configuration-only/token rotations also create a fresh revision. It then requires:

1. Bounded `/health` success (cold-start retries).
2. Anonymous `/threads` denied with 401/403.
3. Authorized `POST /admin/verify-models` returning **`ready: true`**, with nonempty results for all configured routes.

The probe runs **inside the API**, sharing its actual model client/configuration and managed identity. It makes paid inference calls sequentially, with per-call and total deadlines; only one verification can run at once. Empty responses, wrong/malformed tool calls, absent required arguments, missing deployments, bad credentials, and timeouts do not pass readiness. HTTP 200 alone is insufficient.

Failed verification fails the workflow, but **does not roll the image back**. The app remains protected by the owner gate. Diagnose the results, fix configuration/permissions/models, and run action **verify** to recheck without rebuilding:

```bash
gh workflow run backend.yml -R zzurlo/agent-harness --ref main -f action=verify
```

Infrastructure and backend deployment jobs serialize through the same GitHub concurrency group; in-progress deployments are not cancelled.

## 6. Deploy frontend and do a manual end-to-end pass

Actions → **Deploy frontend** → Run workflow → **main**. The workflow tests/builds, copies `staticwebapp.config.json`, and uploads `frontend/dist` as a prebuilt app. `VITE_API_BASE` is public and is the only deployment URL baked into the bundle.

```bash
gh workflow run frontend.yml -R zzurlo/agent-harness --ref main
```

Open the SWA hostname, unlock with the private owner key, then test streaming chat, actual tool execution, approve/deny, thread persistence via the API, and logout. Logout must hide messages/approvals and stop the browser stream. It does not rotate the key, delete saved data, or undo completed tool actions. Reload requires unlocking again.

The thread-list/switcher UI is not implemented yet. Test persistence using authenticated thread APIs, not merely whether a message appeared in the browser. Long-thread Table Storage limits remain follow-up work.

## Rotation, costs and troubleshooting

**Owner key rotation:** update the GitHub `APP_ACCESS_TOKEN` secret, run backend deploy, then unlock using the new key. New requests using the old key must receive 401. In-flight requests authorized before rotation are not a session-revocation system; deployments/restarts may also interrupt pending approvals.

**Costs:** ACR Basic alone was approximately $5 per 30 days at centralus retail pricing in September 2026. ACA min 0 reduces idle compute; it does not make the entire stack free. Logs/storage/inference/registry tasks are additional usage. The log daily quota is not a hard monthly dollar cap; configure budget alerts. No total monthly cost is guaranteed.

| Symptom | Check |
|---|---|
| PR/main merge has skipped deploy jobs | Expected: manual main-only dispatch is required |
| OIDC login fails | Main ref, exact federated subject, tenant/subscription and resource-group roles |
| Provider not registered | Subscription administrator registers required providers; don't widen CI roles |
| First app doesn't become ready | Bootstrap must listen on 80; inspect provisioning status/Activity Log |
| Image pull failure | AcrPull propagation and explicit system-identity ACR registry binding |
| Failed/pending latest revision on infra rerun | Helper refuses ambiguous state; resolve failed rollout first |
| UI says private access unconfigured | Set strong APP_ACCESS_TOKEN and deploy a new revision |
| Browser CORS errors | Exact SWA origin in runtime CORS_ORIGINS; no wildcard; check API auth separately |
| `/health` works but chat fails | Health doesn't test models; use runtime verification results |
| Model verification fails/times out | Deployment names/capabilities, endpoint, API identity scope, quota and reasoning output budget |
| Long conversation won't persist | Existing single-string Table Storage representation can hit property limits |
| `Azure operation failed (output withheld)` | Activity Log/Deployment operations in Azure; output is suppressed to avoid leaking secrets |

Never declare deployment complete from a green build alone. Cloud model access, persisted conversations and live UI behavior remain unverified until the explicit post-provisioning checks succeed.
