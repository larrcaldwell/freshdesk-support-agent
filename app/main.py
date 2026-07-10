"""FastAPI app: receives Freshdesk automation webhooks and processes tickets.

Endpoints:
  POST /webhook/ticket   — called by a Freshdesk automation rule on ticket creation
                           (and optionally on customer replies)
  GET  /health           — liveness + config check
"""
from __future__ import annotations

import hmac
import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from . import store
from .config import settings
from .dashboard import router as dashboard_router
from .knowledge import load_docs
from .pipeline import process_ticket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

app = FastAPI(title="Freshdesk Support Agent")
app.include_router(dashboard_router)


@app.on_event("startup")
def startup() -> None:
    problems = settings.validate()
    for p in problems:
        log.error("CONFIG: %s", p)
    n = load_docs()
    log.info(
        "Started. auto_reply=%s min_confidence=%s categories=%s docs=%d",
        settings.auto_reply_enabled,
        settings.auto_reply_min_confidence,
        settings.auto_reply_categories,
        n,
    )


@app.get("/health")
def health() -> dict:
    return {
        "ok": not settings.validate(),
        "problems": settings.validate(),
        "auto_reply_enabled": settings.auto_reply_enabled,
    }


@app.post("/webhook/ticket")
async def ticket_webhook(
    request: Request,
    background: BackgroundTasks,
    x_webhook_secret: str | None = Header(default=None),
) -> dict:
    if not settings.webhook_secret or not hmac.compare_digest(
        x_webhook_secret or "", settings.webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Bad webhook secret")

    payload = await request.json()
    # Freshdesk automation "Trigger webhook" with custom JSON:
    #   {"ticket_id": "{{ticket.id}}"}
    raw_id = payload.get("ticket_id") or (payload.get("freshdesk_webhook") or {}).get("ticket_id")
    try:
        ticket_id = int(raw_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"Missing/invalid ticket_id in payload: {payload}")

    background.add_task(_safe_process, ticket_id)
    return {"accepted": True, "ticket_id": ticket_id}


def _safe_process(ticket_id: int) -> None:
    try:
        process_ticket(ticket_id)
    except Exception as e:
        log.exception("Failed processing ticket #%s", ticket_id)
        try:
            store.record(ticket_id, action="error", detail=str(e))
        except Exception:
            pass
