"""Team dashboard: GET /dashboard

Access: append ?key=YOUR_DASHBOARD_KEY once; a cookie keeps you signed in
after that, so the team can bookmark the plain /dashboard URL.
"""
from __future__ import annotations

import html
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from . import store
from .config import settings

router = APIRouter()

ACTION_LABELS = {
    "auto-replied": ("Auto-replied", "#1665c0", "#e3effd"),
    "draft-posted": ("Draft ready", "#1e7b34", "#e2f4e6"),
    "triage-only": ("Triaged", "#6c5ce7", "#eeeafd"),
    "error": ("Error", "#c0392b", "#fdeaea"),
}


def _fmt_time(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    mins = int((time.time() - ts) / 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 24 * 60:
        return f"{mins // 60} hr {mins % 60} min ago"
    return dt.strftime("%b %d, %H:%M UTC")


def _badge(action: str) -> str:
    label, fg, bg = ACTION_LABELS.get(action, (action or "?", "#555", "#eee"))
    return f'<span class="badge" style="color:{fg};background:{bg}">{label}</span>'


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    if not settings.dashboard_key:
        return HTMLResponse("<h3>Dashboard disabled — set DASHBOARD_KEY.</h3>", status_code=503)

    supplied = request.query_params.get("key") or request.cookies.get("fd_agent_key")
    if supplied != settings.dashboard_key:
        return HTMLResponse(
            "<h3 style='font-family:sans-serif'>Access key required.</h3>"
            "<p style='font-family:sans-serif'>Open the dashboard link your admin shared "
            "(it ends in <code>?key=...</code>).</p>",
            status_code=401,
        )

    day = store.counts(24)
    week = store.counts(24 * 7)
    events = store.recent(50)
    fd_url = f"https://{settings.freshdesk_domain}.freshdesk.com/a/tickets"

    rows = []
    for e in events:
        conf = f"{e['confidence']}%" if e["confidence"] is not None else "–"
        human = "Yes" if e["needs_human"] else ("No" if e["needs_human"] is not None else "–")
        detail = html.escape(e["detail"] or "")
        rows.append(
            f"<tr><td>{_fmt_time(e['ts'])}</td>"
            f"<td><a href='{fd_url}/{e['ticket_id']}' target='_blank'>#{e['ticket_id']}</a></td>"
            f"<td class='subj'>{html.escape(e['subject'] or '')}</td>"
            f"<td>{html.escape(e['category'] or '–')}</td>"
            f"<td>{html.escape(e['sentiment'] or '–')}</td>"
            f"<td>{conf}</td><td>{human}</td><td>{_badge(e['action'])}"
            + (f"<div class='detail'>{detail}</div>" if e["action"] == "error" and detail else "")
            + "</td></tr>"
        )
    table = "".join(rows) or "<tr><td colspan='8' class='empty'>No activity since the app last restarted. New tickets will appear here automatically.</td></tr>"

    mode = (
        "<span class='badge' style='color:#c0392b;background:#fdeaea'>AUTO-REPLY ON</span>"
        if settings.auto_reply_enabled
        else "<span class='badge' style='color:#1e7b34;background:#e2f4e6'>Draft mode — humans send every reply</span>"
    )

    def stat(label: str, value) -> str:
        return f"<div class='card'><div class='num'>{value}</div><div class='lbl'>{label}</div></div>"

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Support Agent — truDigital</title>
<style>
 body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin:0; background:#f5f7f6; color:#222; }}
 header {{ background:#153d2e; color:#fff; padding:16px 28px; display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
 header h1 {{ font-size:18px; margin:0; font-weight:600; }}
 header .sub {{ opacity:.75; font-size:13px; }}
 .wrap {{ max-width:1150px; margin:22px auto; padding:0 16px; }}
 .cards {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:22px; }}
 .card {{ background:#fff; border-radius:10px; padding:16px 22px; box-shadow:0 1px 3px rgba(0,0,0,.08); min-width:130px; }}
 .num {{ font-size:26px; font-weight:700; color:#153d2e; }}
 .lbl {{ font-size:12px; color:#667; margin-top:2px; }}
 table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
 th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.4px; color:#889; padding:10px 12px; border-bottom:2px solid #eef; }}
 td {{ padding:10px 12px; border-bottom:1px solid #f0f2f1; font-size:13.5px; vertical-align:top; }}
 tr:hover td {{ background:#fafcfb; }}
 a {{ color:#1665c0; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
 .badge {{ padding:3px 9px; border-radius:20px; font-size:12px; font-weight:600; white-space:nowrap; }}
 .subj {{ max-width:320px; }}
 .detail {{ color:#c0392b; font-size:12px; margin-top:4px; }}
 .empty {{ text-align:center; color:#889; padding:28px; }}
 footer {{ color:#99a; font-size:12px; margin:18px 4px; }}
</style></head>
<body>
<header><h1>🤖 AI Support Agent</h1>{mode}
 <span class="sub">Model: {html.escape(settings.model)} · Confidence bar: {settings.auto_reply_min_confidence}% · Page refreshes every 60s</span>
</header>
<div class="wrap">
 <div class="cards">
  {stat("Tickets handled · 24h", day.get("total", 0))}
  {stat("Drafts ready · 24h", day.get("draft-posted", 0))}
  {stat("Auto-replied · 24h", day.get("auto-replied", 0))}
  {stat("Errors · 24h", day.get("error", 0))}
  {stat("Handled · 7 days", week.get("total", 0))}
 </div>
 <table>
  <tr><th>When</th><th>Ticket</th><th>Subject</th><th>Category</th><th>Sentiment</th><th>Confidence</th><th>Needs human</th><th>Result</th></tr>
  {table}
 </table>
 <footer>Click a ticket number to open it in Freshdesk — the agent's triage note and draft reply are in the ticket as a private note.
 History resets if the app restarts (free hosting tier). "Needs human: Yes" = the agent wants one of you to review before anything goes out.</footer>
</div></body></html>"""

    resp = HTMLResponse(body)
    if request.query_params.get("key") == settings.dashboard_key:
        resp.set_cookie("fd_agent_key", settings.dashboard_key, max_age=60 * 60 * 24 * 90, httponly=True)
    return resp
