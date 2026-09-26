# agent-harness

A ChatGPT-style chat app built on a **hand-written agent harness** over Microsoft Foundry models.

The point of this repo isn't the chat UI — it's the harness. The model just emits tokens; everything that turns that into a useful assistant is code you own:

- the **loop** (model → tool calls → results → model)
- **context compaction** when history outgrows the window
- **budgets** so a confused model can't run forever
- **structured error recovery** so tool failures are recoverable, not fatal
- **approval gates** for dangerous tools
- **cost-aware routing** across model tiers

## Cost posture

Built to run for **~$5–25/month**.

| Component | Choice | Cost |
|---|---|---|
| Frontend | Azure Static Web Apps, **Free** tier | $0 |
| Backend | Container Apps, Consumption, **min-replicas 0** | $0 idle, ~$0–5/mo |
| Inference | Foundry serverless, tiered routing | ~$2–15/mo |
| Threads | Azure Table Storage | pennies |
| Logs | Log Analytics, capped 0.5 GB/day | $0–3/mo |

Two deliberate omissions:

- **No Postgres.** A B1ms Flexible Server is ~$13/mo — more than everything else combined. Table Storage does the job.
- **No SWA Standard tier.** Its "linked backend" proxy costs $9/mo; the frontend calls the Container App directly with CORS instead.

### Cold starts

`minReplicas: 0` means ~$0 at idle and a 3–10s cold start. The frontend fires `GET /warmup` on page load, so the container is warming while the user types their first message. The dot in the header shows warm state.

## Model routing

Every turn picks the cheapest model that can actually do the job. This is the biggest cost lever in the project.

| Route | Default model | $/1M in | $/1M out | When |
|---|---|---|---|---|
| `fast` | Phi-4-mini-reasoning | 0.08 | 0.32 | titles, compaction summaries, trivial turns |
| `chat` | Llama-3.3-70B-Instruct | 0.20 | 0.60 | **default** — non-reasoning conversation |
| `tools` | grok-4.1-fast-reasoning | 0.20 | 0.50 | any turn with tool calls |
| `think` | DeepSeek-V4-Flash | 0.19 | 0.51 | "think hard", or >100k token history (1M ctx) |
| `max` | DeepSeek-V3.2 | 0.58 | 1.68 | opt-in escalation only |

Swap any of these with `MODEL_FAST`, `MODEL_CHAT`, … env vars — no code changes.

**Why `chat` is not a reasoning model:** reasoning models bill hidden thinking tokens at the output rate and add seconds of latency. Most turns are conversation, not proof-solving. Routing them to a plain instruct model is worth more than any other optimization here.

**Why `tools` is Grok 4.1 Fast Reasoning:** it's cheap *and* explicitly tool-calling capable. Models that silently ignore tool schemas will waste your afternoon. The harness only advertises tools to routes flagged `supports_tools`.

> Prices are directional. Verify in the Foundry catalog for your region at deploy time — serverless pricing varies by region and changes.

## Architecture

```
┌──────────────────────────────────────────┐
│  React / Vite on Static Web Apps (Free)  │
│   warmup ping · SSE stream · approvals   │
└───────────────┬──────────────────────────┘
                │ POST /chat (CORS, SSE)
┌───────────────▼──────────────────────────┐
│  Container Apps · min-replicas 0         │
│  ┌────────────────────────────────────┐  │
│  │ HARNESS                            │  │
│  │  router   → cheapest capable tier  │  │
│  │  loop     → model ⇄ tools          │  │
│  │  context  → compaction             │  │
│  │  budgets  → calls/time/tokens      │  │
│  │  registry → schemas + safe dispatch│  │
│  └────────────────────────────────────┘  │
└───────────────┬──────────────────────────┘
                │ OpenAI-compatible v1 API
┌───────────────▼──────────────────────────┐
│  Microsoft Foundry — serverless models   │
└──────────────────────────────────────────┘
```

## Layout

```
backend/
  harness/
    config.py       routes, budgets, context policy (all env-overridable)
    router.py       route selection
    loop.py         THE LOOP — streaming, tools, budgets, approvals
    context.py      token accounting + compaction
    persistence.py  Table Storage / in-memory threads
    providers/
      foundry.py    Foundry client (API key or managed identity)
    tools/
      registry.py   decorator registration, schema gen, safe dispatch
      builtin.py    example tools
  api/main.py       FastAPI + SSE
  tests/            21 tests, no network required
frontend/           React + Vite
infra/main.bicep    RG-scoped: ACA env, SWA, ACR, Storage, capped logs
.github/workflows/  OIDC deploy for backend + frontend
```

## Local development

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # fill in FOUNDRY_PROJECT_ENDPOINT
uvicorn api.main:app --reload --port 8000

# Frontend (separate shell)
cd frontend
npm install
npm run dev                   # http://localhost:5173
```

Tests need no Foundry deployment:

```bash
cd backend && pytest -q       # 21 passed
```

## Deploy

```bash
az group create -n rg-agent-harness -l centralus

az deployment group create \
  -g rg-agent-harness \
  -f infra/main.bicep \
  -p foundryProjectEndpoint="https://<res>.services.ai.azure.com/api/projects/<proj>" \
     corsOrigins="https://<your-swa>.azurestaticapps.net"
```

Then set repo secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `ACR_NAME`, `AZURE_STATIC_WEB_APPS_API_TOKEN`, `VITE_API_BASE`.

The Container App's managed identity needs the **Foundry User** role on the Foundry project (note: the Azure AI User/Owner roles were renamed to Foundry User/Owner; role IDs are unchanged).

## Adding a tool

```python
from harness.tools.registry import registry

@registry.register()
def lookup_order(order_id: str) -> dict:
    """Look up an order by ID."""      # docstring -> tool description
    return {"id": order_id, "status": "shipped"}

@registry.register(dangerous=True)     # pauses loop, asks the UI first
def refund(order_id: str, amount: float) -> dict:
    """Issue a refund."""
    ...
```

The JSON schema is derived from type hints; required params come from which args lack defaults.

## Design notes

**Tool errors are data, not exceptions.** `dispatch()` never raises — it returns `{"error": ..., "retryable": bool}`. A model that sees a structured error adapts; a model that sees a stack trace flails.

**Compaction never orphans tool messages.** The split point walks backwards past `tool` messages and their parent `tool_calls` assistant message. Splitting mid-group produces payloads the API rejects — there's a test for this.

**Budgets are hard stops.** Max tool calls per turn, wall-clock, tokens, and per-tool timeout. Exceeding one emits `budget_exceeded` and closes the stream cleanly rather than hanging.

**Tools are only advertised to capable routes.** Reasoning-tagged ≠ tool-calling-capable.

## Roadmap

- [ ] Microsoft Agent Framework adapter as an alternate provider (it's a supported framework for Foundry **hosted agents**, which would also give per-agent Entra identity and session state)
- [ ] Prompt caching hints for models that support it — the system prompt + history is most of your input spend
- [ ] Streaming tool results
- [ ] Thread list / switcher in the UI
- [ ] Entra auth via SWA's built-in provider (free on the Free tier)
