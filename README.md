# agent-harness

A private, single-owner ChatGPT-style app with a **hand-written agent harness** over Microsoft Foundry models. React/Vite frontend, FastAPI/SSE backend, Azure Table Storage for conversations. This is not a Microsoft Agent Framework implementation; an optional adapter remains future work.

The harness owns model/tool execution, context compaction, per-turn budgets, structured tool errors, approval gates, and tiered routing. No OpenAI/Anthropic model deployment is required: the OpenAI **client library/protocol** is used to call compatible Foundry-hosted models.

## Deployment status and safety

Build/test success is not a live deployment. Follow **[docs/DEPLOY.md](docs/DEPLOY.md)** for the ordered setup and verification gates.

- PRs and main pushes run tests/builds only. **All Azure deployments require manual workflow dispatch from `main`.** Merging does not provision resources.
- Infrastructure defaults to **what-if**, not deploy. Privileged PR previews are disabled; GitHub OIDC trusts main only.
- Use `scripts/deploy.py` for infrastructure reruns. It preserves the live image and runtime configuration, rather than restoring a bootstrap image.
- Bootstrap serves the public quickstart on port 80. The real backend serves on 8000, using its system identity for ACR pulls.
- `minReplicas=0`, `maxReplicas=1`, one worker and Single revision mode: approvals are currently process-local. Do not scale out until approval state is shared.

## Private access

Set a cryptographically random **`APP_ACCESS_TOKEN` of at least 32 non-whitespace characters** on the backend. Enter the same key into the browser's unlock form. This is one owner's private workspace, not multi-user accounts.

- All API routes (including thread read/delete, approvals, docs and model verification) require `Authorization: Bearer ...`.
- Only GET `/health`, GET `/warmup`, and valid CORS preflights are public.
- Missing/short configuration fails closed with 503; invalid credentials receive 401.
- The browser keeps the key in memory only, never localStorage/sessionStorage or the build. Refresh requires unlocking again. Logout aborts requests and clears private UI state; it does not delete stored threads or rotate the server key.
- **Never put the key in `VITE_*`, URLs, git, screenshots, or chat messages.** Use HTTPS outside local development. Backend CORS is limited to the explicit frontend origin, but CORS is not authentication.

## Foundry configuration

Use **`FOUNDRY_MODEL_ENDPOINT`** with the direct resource model URL copied from the deployment, for example:

```text
https://<resource>.openai.azure.com/openai/v1
```

A project URL such as `/api/projects/<project>` is **not** a Chat Completions model endpoint. The legacy `FOUNDRY_PROJECT_ENDPOINT` variable is accepted only if it already contains a valid model URL, with a deprecation warning. API keys are optional for local testing; production uses an async, refreshing managed-identity credential.

Five deployment names are configurable: `MODEL_FAST`, `MODEL_CHAT`, `MODEL_TOOLS`, `MODEL_THINK`, `MODEL_MAX`. Keep `chat` non-reasoning for cost and latency; `tools` must actually call functions. Code defaults are examples, **not verified availability or pricing**. Production deployment requires explicit names for all five; several tiers may point at the same compatible model. Capability flags/output budgets/prices remain in `backend/harness/config.py` and must agree with the chosen models.

The protected `POST /admin/verify-models` checks the **running backend's identity and configuration**, sequentially and with bounded request/overall budgets. It returns `{ready, results}`. `ready: false` is a failure even with HTTP 200. This is a paid inference probe. Deployment verification requires health, denied anonymous thread access, and all configured routes ready; CI's deployment identity is never substituted for runtime credentials.

## Local development

Python 3.11+ and Node `^20.19.0 || >=22.12.0`. Commands below are Bash (Linux, macOS, or Git Bash with appropriate Python/venv paths):

```bash
cd backend
python -m venv .venv
# On Git Bash/Windows: source .venv/Scripts/activate
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
# Edit .env locally: real model endpoint/names, optional API key, private owner key.
# The app reads process environment; copying .env alone does not load it.
set -a; source .env; set +a
uvicorn api.main:app --reload --port 8000
```

In another terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173` and unlock. The warmup request starts the backend before your first message. Local model verification: `cd backend && python scripts/verify_models.py --json`; route filtering, `--timeout`, and `--total-timeout` are supported. `--skip-tools` is diagnostic only, not a production readiness check.

## Tests

```bash
cd backend
pip install -e '.[dev]'
pytest -q
ruff check .
cd ..
pip install pytest pyyaml
python -m pytest tests -q
az bicep build --file infra/main.bicep --outfile /tmp/main.json
az bicep lint --file infra/main.bicep
cd frontend
npm ci
npm test
npm run build
```

Docker dependencies come from the same `pyproject.toml` as CI, including the async identity transport and packaged runtime verifier.

## Cost posture

- SWA Free; ACA Consumption scaled to zero when idle.
- **ACR Basic has a paid baseline**: September 2026 centralus retail pricing was USD 0.1666/day, about USD 5 for 30 days, before extra usage. Recheck Azure pricing.
- Table Storage, inference, registry tasks, and logs are usage-billed. No Postgres.
- Log Analytics has a 0.5 GB/day ingestion quota, **not a hard monthly dollar ceiling**. Configure Azure budget alerts and verify actual spending. Earlier blanket `$5–25/month` estimates are not a guarantee.

## Known limits / next work

- Authentication is a single shared owner key, not Entra user login or per-user thread isolation.
- Thread switcher, prompt-cache optimization, distributed approvals and streaming tool results remain future work.
- Pending approvals do not survive restarts; single-replica operation avoids cross-replica routing but not restart loss.
- Conversation persistence still stores one JSON string per thread. Long conversations can hit Azure Table property limits; storage reliability/large-thread handling needs further work.
- Unit tests and local container probes do not validate Azure permissions, regional capacity, Foundry model support, or end-to-end cloud persistence. Those gates run after deliberate provisioning.
