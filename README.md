# Freshdesk Support Agent

An AI agent that handles your support tickets — email and chat-widget conversations alike — through Freshdesk. For every incoming ticket it:

1. **Triages** — classifies category, priority, sentiment; adds tags; optionally routes to the right group.
2. **Researches** — searches your Freshdesk knowledge base, your internal docs (`knowledge/` folder), and past resolved tickets.
3. **Responds** — auto-replies to simple tickets it's confident about, or posts a draft reply as a private note for your team to review and send.

Every action leaves a private triage note on the ticket explaining what the agent decided and why.

## Safety model

The agent ships in **draft-only mode** (`AUTO_REPLY_ENABLED=false`). Nothing customer-facing is sent until you flip the switch. Even then, an auto-reply only goes out if all of these hold: the agent didn't flag it for a human, confidence ≥ `AUTO_REPLY_MIN_CONFIDENCE`, the category is on your allowlist, the customer isn't frustrated/angry, and the agent hasn't already auto-replied on that ticket. Refunds, credits, account changes, and legal/security issues are always flagged for a human.

## Setup

### 1. Configure

```bash
cp .env.example .env
```

Fill in:

| Variable | Where to get it |
|---|---|
| `FRESHDESK_DOMAIN` | The `yourcompany` part of yourcompany.freshdesk.com |
| `FRESHDESK_API_KEY` | Freshdesk → click your avatar → Profile Settings → API Key. Use a dedicated agent account (e.g. "AI Assistant") so bot replies are attributed clearly. |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `WEBHOOK_SECRET` | Any long random string, e.g. `openssl rand -hex 32` |

### 2. Add knowledge

Drop product docs, FAQs, and policies as `.md`/`.txt` files into `knowledge/`. The agent also searches your published Freshdesk Solutions articles and past tickets automatically.

### 3. Run

```bash
docker build -t fd-agent .
docker run --env-file .env -p 8000:8000 fd-agent
```

Or without Docker: `pip install -r requirements.txt && uvicorn app.main:app --port 8000`.

Check `GET /health` — it reports any missing config.

The app must be reachable from the internet (deploy to Fly.io, Railway, Render, a VPS, etc.). For local testing, tunnel with `ngrok http 8000`.

### 4. Wire up Freshdesk

Create an automation rule: **Admin → Workflows → Automations → Ticket Creation → New Rule**.

- **Conditions**: e.g. Source is Email OR Chat (or leave broad).
- **Actions**: **Trigger Webhook**
  - Request type: `POST`
  - URL: `https://your-app.example.com/webhook/ticket`
  - Custom headers: `X-Webhook-Secret: <your WEBHOOK_SECRET>`
  - Encoding: JSON, Content: Advanced →
    ```json
    {"ticket_id": "{{ticket.id}}"}
    ```

Optionally add a second rule under **Ticket Updates** with condition "Reply is sent by requester" and the same webhook, so the agent also handles customer follow-ups (chat-widget conversations arrive as replies on the same ticket).

**Important**: add a condition so the rule skips tickets updated by your bot agent account, or you may loop.

## How chats are handled

Freshdesk's chat/help widget conversations arrive as tickets with `source: chat`. The agent detects this and writes shorter, more conversational replies. Because it's webhook-driven, response latency is typically 10–30 seconds — fine for the widget's asynchronous messaging model, but this is not a live-typing chatbot.

## Rollout suggestion

1. Week 1–2: run in draft mode. Review the private-note drafts and triage quality.
2. Tune `knowledge/`, `AUTO_REPLY_CATEGORIES`, and the system prompt in `app/agent.py` based on what you see.
3. Enable `AUTO_REPLY_ENABLED=true` for one or two safe categories, with high `AUTO_REPLY_MIN_CONFIDENCE`.
4. Expand the allowlist as trust grows.

## Project layout

```
app/
  main.py       FastAPI webhook receiver
  pipeline.py   Orchestration + auto-reply safety gates
  agent.py      Claude agent loop, tools, system prompt (edit voice/rules here)
  freshdesk.py  Freshdesk API v2 client
  knowledge.py  Local docs search
  config.py     All settings/env vars
knowledge/      Your docs (.md/.txt)
```

## Notes

- Rate limits: the client retries on Freshdesk 429s automatically.
- `search/solutions` requires certain Freshdesk plans; if unavailable the agent falls back to local docs and past tickets.
- Cost: roughly one Claude agent run (a few model calls) per inbound ticket/reply.
