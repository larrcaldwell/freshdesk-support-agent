"""FastAPI app: receives Freshdesk automation webhooks and processes tickets.

Endpoints:
  POST /webhook/ticket   — called by a Freshdesk automation rule on ticket creation
                           (and optionally on customer replies)
  GET  /health           — liveness + config check
"""
from __future__ import annotations

import hmac
import logging

from fastapi import BackgroundTasks, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import freshchat, store, training
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


@app.post("/feedback")
async def feedback(
    request: Request,
    ticket_id: int = Form(0),
    subject: str = Form(""),
    correction: str = Form(...),
    author: str = Form(""),
) -> RedirectResponse:
    """Teach-the-agent form on the dashboard. Auth via the dashboard cookie."""
    supplied = request.cookies.get("fd_agent_key") or request.query_params.get("key") or ""
    if not settings.dashboard_key or supplied != settings.dashboard_key:
        raise HTTPException(status_code=401, detail="Dashboard key required")
    correction = correction.strip()
    if correction:
        store.record_feedback(ticket_id, subject, correction, author.strip())
        try:
            training.add_correction(correction, context_subject=subject, ticket_ref=ticket_id, author=author.strip())
        except Exception:
            log.exception("Failed to persist teaching to Freshdesk (kept locally)")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/webhook/freshchat")
async def freshchat_webhook(request: Request, background: BackgroundTasks) -> dict:
    """Freshchat conversation webhook. Freshchat can't send custom headers, so the
    shared secret rides in the query string: /webhook/freshchat?key=WEBHOOK_SECRET"""
    supplied = request.query_params.get("key") or ""
    if not settings.webhook_secret or not hmac.compare_digest(supplied, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="Bad webhook key")

    payload = await request.json()
    action = payload.get("action")
    actor_type = (payload.get("actor") or {}).get("actor_type")
    message = (payload.get("data") or {}).get("message") or {}
    conversation_id = message.get("conversation_id")

    if action == "message_create" and actor_type == "user" and conversation_id:
        background.add_task(_safe_chat, str(conversation_id))
        return {"accepted": True, "conversation_id": conversation_id}
    return {"accepted": False, "ignored": action or "unknown"}


def _safe_chat(conversation_id: str) -> None:
    try:
        freshchat.process_chat_message(conversation_id)
    except Exception as e:
        log.exception("Failed processing chat %s", conversation_id)
        try:
            store.record(0, subject=f"[live chat] {conversation_id}", action="error", detail=str(e))
        except Exception:
            pass


def _safe_process(ticket_id: int) -> None:
    try:
        process_ticket(ticket_id)
    except Exception as e:
        log.exception("Failed processing ticket #%s", ticket_id)
        try:
            store.record(ticket_id, action="error", detail=str(e))
        except Exception:
            pass
