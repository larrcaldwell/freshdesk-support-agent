"""Weekly self-learning scan.

Once a week (or on demand from the Teachings page) the agent reviews the past
week's tickets — especially cases where a human sent something different from
the AI draft — and distills up to 5 new teaching rules. Each rule is stored
through the normal teaching pipeline (a [teach] note on the AI Training Log
ticket, authored "auto-learn") so it shows on the Teachings page and applies
to every future draft. Nothing customer-facing happens here.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import training
from .config import settings
from .freshdesk import fd, strip_html

log = logging.getLogger("selflearn")

WEEK = 7 * 24 * 3600
MAX_TICKETS = 25
MAX_NEW_TEACHINGS = 5
_lock = threading.Lock()
_running = False


def _state_path() -> Path:
    return Path(os.environ.get("DATA_DIR", "/tmp")) / "selflearn.json"


def status() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return {}


def _save_state(**kw) -> None:
    s = status()
    s.update(kw)
    try:
        _state_path().write_text(json.dumps(s))
    except Exception:
        log.exception("Could not save selflearn state")


def is_running() -> bool:
    return _running


def _gather_corpus() -> str:
    """Recent tickets rendered as compact case studies for the model."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tickets = fd._request("GET", f"/tickets?updated_since={since}&per_page={MAX_TICKETS}&order_by=updated_at&order_type=desc") or []
    cases = []
    for t in tickets:
        if "ai-training-log" in (t.get("tags") or []):
            continue
        try:
            full = fd.get_ticket(t["id"])
        except Exception:
            continue
        convo = full.get("conversations") or []
        ai_note = next((strip_html(c.get("body")) for c in convo
                        if c.get("private") and "[ai-agent]" in (c.get("body") or "")), "")
        human_replies = [strip_html(c.get("body")) for c in convo
                         if not c.get("incoming") and not c.get("private")
                         and "[ai-agent]" not in (c.get("body") or "")]
        if not ai_note:
            continue
        case = [f"--- Ticket #{full['id']}: {full.get('subject')}",
                f"Customer wrote: {strip_html(full.get('description'))[:600]}",
                f"AI triage/draft: {ai_note[:900]}"]
        if human_replies:
            case.append(f"What the human team actually sent: {human_replies[-1][:900]}")
        case.append(f"Final status: {full.get('status')}")
        cases.append("\n".join(case))
        if len(cases) >= 15:
            break
    return "\n\n".join(cases)


PROMPT = """You are reviewing one week of support tickets for truDigital Signage to make
the AI support agent better. Below are cases showing the customer's message, the AI
agent's draft, and (when present) what the human team actually sent instead.

Extract AT MOST {n} concise, generalizable teaching rules the agent should follow going
forward. Focus on: differences between AI drafts and what humans sent, recurring product
facts the AI didn't know, tone corrections, and repeated issue patterns. Each rule must be
one or two sentences, self-contained, and actionable. Do NOT restate rules that are obvious
or generic ("be polite"). If there is nothing genuinely new to learn, return an empty list.

Respond with ONLY a JSON array of strings, e.g. ["rule one", "rule two"].

CASES:
{cases}"""


def run_scan(trigger: str = "weekly") -> dict:
    """Run one scan. Returns a summary dict. Never raises."""
    global _running
    with _lock:
        if _running:
            return {"ok": False, "message": "A scan is already running."}
        _running = True
    try:
        corpus = _gather_corpus()
        if not corpus.strip():
            _save_state(last_run=time.time(), last_result="No AI-handled tickets found this week.", last_added=0)
            return {"ok": True, "added": 0, "message": "No AI-handled tickets found this week."}

        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model=settings.model,
            max_tokens=1200,
            messages=[{"role": "user", "content": PROMPT.format(n=MAX_NEW_TEACHINGS, cases=corpus[:60000])}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        start, end = text.find("["), text.rfind("]")
        rules = json.loads(text[start:end + 1]) if start != -1 and end != -1 else []
        rules = [str(r).strip() for r in rules if str(r).strip()][:MAX_NEW_TEACHINGS]

        existing = " ".join(training.load_corrections()).lower()
        added = 0
        for rule in rules:
            if rule.lower()[:60] in existing:  # crude dedupe
                continue
            training.add_correction(rule, context_subject="Weekly self-learning scan", author="auto-learn")
            added += 1
        msg = f"Reviewed the week's tickets; added {added} new teaching{'s' if added != 1 else ''}."
        _save_state(last_run=time.time(), last_result=msg, last_added=added, trigger=trigger)
        log.info("Self-learn scan (%s): %s", trigger, msg)
        return {"ok": True, "added": added, "message": msg}
    except Exception as e:
        log.exception("Self-learn scan failed")
        _save_state(last_run=time.time(), last_result=f"Scan failed: {e}", last_added=0)
        return {"ok": False, "message": str(e)[:200]}
    finally:
        _running = False


def start_weekly_loop() -> None:
    """Background thread: checks hourly, runs when a week has passed."""
    def loop():
        time.sleep(120)  # let the app settle after boot
        while True:
            try:
                last = status().get("last_run", 0)
                if time.time() - last >= WEEK:
                    run_scan("weekly")
            except Exception:
                log.exception("Self-learn loop error")
            time.sleep(3600)

    threading.Thread(target=loop, daemon=True, name="selflearn").start()
