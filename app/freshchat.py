"""Freshchat live-chat copilot.

When a customer sends a message in the chat widget, Freshchat fires a webhook
to /webhook/freshchat. We fetch the conversation transcript, ask the agent for
a suggested reply, and post it back into the conversation as a PRIVATE note
(message_type=private) — visible to the human agent, never to the customer.

Requires env: FRESHCHAT_API_URL (e.g. https://trudigital.freshchat.com/v2)
and FRESHCHAT_API_TOKEN (Freshchat Admin > API Tokens).
"""
from __future__ import annotations

import logging
import time

import httpx

from . import store
from .config import settings

log = logging.getLogger("freshchat")

_client: httpx.Client | None = None
_default_agent_id: str | None = None
_last_run: dict[str, float] = {}  # conversation_id -> monotonic ts (throttle)

THROTTLE_SECONDS = 20


def enabled() -> bool:
    return bool(settings.freshchat_api_url and settings.freshchat_api_token)


def _c() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            base_url=settings.freshchat_api_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {settings.freshchat_api_token}",
                "Content-Type": "application/json",
                "accept": "application/json",
            },
            timeout=30,
        )
    return _client


def _get(path: str, **params) -> dict:
    r = _c().get(path, params=params or None)
    if r.status_code >= 400:
        log.error("Freshchat GET %s -> %s: %s", path, r.status_code, r.text[:300])
    r.raise_for_status()
    return r.json()


def _default_agent() -> str | None:
    global _default_agent_id
    if _default_agent_id is None:
        try:
            data = _get("/agents", items_per_page=1)
            agents = data.get("agents") or []
            _default_agent_id = agents[0]["id"] if agents else None
        except Exception:
            log.exception("Could not list Freshchat agents")
    return _default_agent_id


def _transcript(conversation_id: str) -> tuple[str, str | None, bool]:
    """Returns (transcript_text, assigned_agent_id, is_resolved)."""
    convo = _get(f"/conversations/{conversation_id}")
    resolved = convo.get("status") == "resolved"
    agent_id = convo.get("assigned_agent_id") or None

    data = _get(f"/conversations/{conversation_id}/messages", items_per_page=50)
    msgs = data.get("messages") or []
    msgs.sort(key=lambda m: m.get("created_time", ""))

    lines = []
    for m in msgs:
        if m.get("message_type") == "private":
            continue  # skip our own notes and agent whispers
        who = {"user": "CUSTOMER", "agent": "AGENT", "bot": "BOT", "system": "SYSTEM"}.get(
            m.get("actor_type"), "?"
        )
        for part in m.get("message_parts") or []:
            text = (part.get("text") or {}).get("content")
            if text:
                lines.append(f"{who}: {text}")
    return "\n".join(lines), agent_id, resolved


def post_private_note(conversation_id: str, text: str, agent_id: str | None) -> None:
    actor = agent_id or _default_agent()
    if not actor:
        raise RuntimeError("No Freshchat agent id available for posting the note")
    body = {
        "message_parts": [{"text": {"content": text}}],
        "message_type": "private",
        "actor_type": "agent",
        "actor_id": actor,
    }
    r = _c().post(f"/conversations/{conversation_id}/messages", json=body)
    if r.status_code >= 400:
        log.error("Freshchat POST note -> %s: %s", r.status_code, r.text[:300])
    r.raise_for_status()


def process_chat_message(conversation_id: str) -> None:
    """Entry point from the webhook (background task)."""
    if not enabled():
        log.warning("Freshchat copilot called but not configured")
        return

    now = time.monotonic()
    if now - _last_run.get(conversation_id, 0) < THROTTLE_SECONDS:
        log.info("Chat %s throttled", conversation_id)
        return
    _last_run[conversation_id] = now

    transcript, agent_id, resolved = _transcript(conversation_id)
    if resolved or not transcript.strip():
        return
    if not transcript.rstrip().splitlines()[-1].startswith("CUSTOMER:"):
        log.info("Chat %s: last message not from customer; skipping", conversation_id)
        return

    from .agent import handle_chat  # late import to avoid cycles

    verdict = handle_chat(transcript)
    reply = (verdict.get("reply") or "").strip()

    note_lines = ["🤖 [ai-agent] Suggested reply (private — customer can't see this):", ""]
    note_lines.append(reply if reply else "(no suggestion — needs a human decision)")
    note_lines.append("")
    note_lines.append(
        f"Category: {verdict.get('category')} | Sentiment: {verdict.get('sentiment')} | "
        f"Confidence: {verdict.get('confidence')}% | Needs human: {verdict.get('needs_human')}"
    )
    if verdict.get("needs_human"):
        note_lines.append(f"Why: {verdict.get('reasoning', '')[:300]}")
    post_private_note(conversation_id, "\n".join(note_lines), agent_id)

    first_customer_line = next(
        (l[10:] for l in transcript.splitlines() if l.startswith("CUSTOMER: ")), ""
    )
    store.record(
        0,
        subject=f"[live chat] {first_customer_line[:120]}",
        category=verdict.get("category") or "",
        priority=verdict.get("priority") or "",
        sentiment=verdict.get("sentiment") or "",
        confidence=verdict.get("confidence"),
        needs_human=verdict.get("needs_human"),
        action="chat-copilot",
    )
    log.info("Chat %s: suggestion posted", conversation_id)
