"""Orchestrates one ticket end-to-end: fetch → agent → triage updates → reply/draft.

Safety model:
- AUTO_REPLY_ENABLED=false (default): every reply is posted as a PRIVATE NOTE draft.
- When enabled, a reply is auto-sent only if ALL hold:
    * agent says needs_human == false
    * confidence >= AUTO_REPLY_MIN_CONFIDENCE
    * category is in AUTO_REPLY_CATEGORIES
    * sentiment is not frustrated/angry
    * the agent has not already auto-replied on this ticket
- Everything else becomes a draft note with the triage verdict attached.
"""
from __future__ import annotations

import html
import json
import logging

from . import store
from .agent import handle_ticket
from .config import settings
from .freshdesk import PRIORITY_IDS, fd

log = logging.getLogger("pipeline")

BOT_MARKER = "[ai-agent]"  # embedded in notes/tags so we can detect our own activity


def _to_html(text: str) -> str:
    paragraphs = [f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in text.split("\n\n") if p.strip()]
    return "".join(paragraphs) or "<p></p>"


def _already_auto_replied(ticket: dict) -> int:
    n = 0
    for c in ticket.get("conversations") or []:
        if not c.get("incoming") and not c.get("private") and BOT_MARKER in (c.get("body") or ""):
            n += 1
    return n


# Freshdesk on this account requires a ticket "Type" on any update; portal and
# chat tickets often arrive without one. Map the agent's category to a valid Type.
CATEGORY_TO_TYPE = {
    "how-to": "Question",
    "account": "Question",
    "order-status": "Question",
    "feature-request": "Question",
    "billing-question": "Billing",
    "refund-request": "Billing",
    "bug-report": "Problem",
    "complaint": "Problem",
}


def _apply_triage(ticket: dict, verdict: dict) -> None:
    if not settings.triage_enabled:
        return
    fields: dict = {}
    if not ticket.get("type"):
        fields["type"] = CATEGORY_TO_TYPE.get(verdict.get("category", ""), "Question")
    prio = PRIORITY_IDS.get(verdict.get("priority", ""))
    if prio and prio != ticket.get("priority"):
        fields["priority"] = prio
    flag_tags = [f"ai-{verdict.get('category', 'other')}"]
    if verdict.get("needs_human"):
        flag_tags.append("needs-human")
    elif verdict.get("reply", "").strip():
        flag_tags.append("ai-draft-ready")
    tags = sorted(set((ticket.get("tags") or []) + (verdict.get("tags") or []) + flag_tags))
    if tags != sorted(ticket.get("tags") or []):
        fields["tags"] = tags
    try:
        routing = json.loads(settings.group_routing_json or "{}")
    except json.JSONDecodeError:
        routing = {}
    group_id = routing.get(verdict.get("category"))
    if group_id and not ticket.get("group_id"):
        fields["group_id"] = int(group_id)
    if fields:
        fd.update_ticket(ticket["id"], **fields)
        log.info("Ticket #%s triaged: %s", ticket["id"], fields)


def _may_auto_reply(ticket: dict, verdict: dict) -> tuple[bool, str]:
    if not settings.auto_reply_enabled:
        return False, "auto-reply disabled"
    if verdict.get("needs_human"):
        return False, "agent flagged needs_human"
    if int(verdict.get("confidence", 0)) < settings.auto_reply_min_confidence:
        return False, f"confidence {verdict.get('confidence')} < {settings.auto_reply_min_confidence}"
    if verdict.get("category", "").lower() not in settings.auto_reply_categories:
        return False, f"category '{verdict.get('category')}' not in auto-reply allowlist"
    if verdict.get("sentiment") in ("frustrated", "angry"):
        return False, f"sentiment is {verdict.get('sentiment')}"
    if _already_auto_replied(ticket) >= settings.max_auto_replies_per_ticket:
        return False, "auto-reply limit reached for this ticket"
    if not verdict.get("reply", "").strip():
        return False, "agent produced no reply"
    return True, "all checks passed"


def process_ticket(ticket_id: int) -> None:
    """Entry point called by the webhook handler (in a background task)."""
    ticket = fd.get_ticket(ticket_id)

    # Skip closed/resolved and spam
    if ticket.get("status") in (4, 5):
        log.info("Ticket #%s is resolved/closed; skipping", ticket_id)
        return
    if ticket.get("spam"):
        log.info("Ticket #%s marked spam; skipping", ticket_id)
        return
    if "ai-training-log" in (ticket.get("tags") or []) or (ticket.get("subject") or "").startswith(
        "AI Agent Training Log"
    ):
        log.info("Ticket #%s is the training log; skipping", ticket_id)
        return

    verdict = handle_ticket(ticket)
    try:
        _apply_triage(ticket, verdict)
    except Exception:
        # Triage is best-effort; never let it block the draft/reply.
        log.exception("Triage update failed for ticket #%s (continuing)", ticket_id)

    reply_text = (verdict.get("reply") or "").strip()
    ok, reason = _may_auto_reply(ticket, verdict)

    triage_note = (
        f"<p><b>{BOT_MARKER} AI triage</b></p>"
        f"<p>Summary: {html.escape(verdict.get('summary', ''))}<br>"
        f"Category: {verdict.get('category')} | Priority: {verdict.get('priority')} | "
        f"Sentiment: {verdict.get('sentiment')} | Confidence: {verdict.get('confidence')}%<br>"
        f"Needs human: {verdict.get('needs_human')}<br>"
        f"Reasoning: {html.escape(verdict.get('reasoning', ''))}</p>"
    )

    if ok:
        fd.reply(ticket_id, _to_html(reply_text) + f"<!-- {BOT_MARKER} -->")
        fd.private_note(ticket_id, triage_note + "<p><i>Reply auto-sent by AI agent.</i></p>")
        action = "auto-replied"
        log.info("Ticket #%s: auto-replied", ticket_id)
    elif reply_text:
        draft = triage_note + f"<p><b>Draft reply (not sent — {html.escape(reason)}):</b></p>" + _to_html(reply_text)
        fd.private_note(ticket_id, draft)
        action = "draft-posted"
        log.info("Ticket #%s: draft posted (%s)", ticket_id, reason)
    else:
        fd.private_note(ticket_id, triage_note + "<p><i>No reply drafted.</i></p>")
        action = "triage-only"
        log.info("Ticket #%s: triage only", ticket_id)

    store.record(
        ticket_id,
        subject=ticket.get("subject") or "",
        category=verdict.get("category") or "",
        priority=verdict.get("priority") or "",
        sentiment=verdict.get("sentiment") or "",
        confidence=verdict.get("confidence"),
        needs_human=verdict.get("needs_human"),
        action=action,
    )
