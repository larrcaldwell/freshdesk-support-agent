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

LOGO_B64 = "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"

ACTION_LABELS = {
    "auto-replied": ("Auto-replied", "#20344c", "#dce8f9"),
    "draft-posted": ("Draft ready", "#08974b", "#e5f9dc"),
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
        else "<span class='badge' style='color:#08974b;background:#e5f9dc'>Draft mode — humans send every reply</span>"
    )

    def stat(label: str, value) -> str:
        return f"<div class='card'><div class='num'>{value}</div><div class='lbl'>{label}</div></div>"

    LOGO_B64_ = LOGO_B64
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Support Agent — truDigital</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
<style>
 body {{ font-family: 'Poppins', -apple-system, Segoe UI, Roboto, sans-serif; margin:0; background:#ebeef2; color:#20344c; }}
 header {{ background:#ffffff; color:#20344c; padding:14px 28px; display:flex; align-items:center; gap:16px; flex-wrap:wrap; border-bottom:4px solid #5cdd31; }}
 header img.logo {{ height:34px; }} header h1 {{ font-size:17px; margin:0; font-weight:600; color:#20344c; }}
 header .sub {{ color:#75808e; font-size:12.5px; }}
 .wrap {{ max-width:1150px; margin:22px auto; padding:0 16px; }}
 .cards {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:22px; }}
 .card {{ background:#fff; border-radius:14px; padding:16px 22px; box-shadow:0 1px 3px rgba(32,52,76,.10); min-width:130px; }}
 .num {{ font-size:26px; font-weight:700; color:#20344c; }}
 .lbl {{ font-size:12px; color:#75808e; margin-top:2px; }}
 table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:14px; overflow:hidden; box-shadow:0 1px 3px rgba(32,52,76,.10); }}
 th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:#75808e; padding:10px 12px; border-bottom:2px solid #ebeef2; font-weight:600; }}
 td {{ padding:10px 12px; border-bottom:1px solid #ebeef2; font-size:13.5px; vertical-align:top; }}
 tr:hover td {{ background:#f4faf1; }}
 a {{ color:#08974b; font-weight:600; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
 .badge {{ padding:3px 9px; border-radius:20px; font-size:12px; font-weight:600; white-space:nowrap; }}
 .subj {{ max-width:320px; }}
 .detail {{ color:#c0392b; font-size:12px; margin-top:4px; }}
 .empty {{ text-align:center; color:#75808e; padding:28px; }}
 footer {{ color:#75808e; font-size:12px; margin:18px 4px; }}
</style></head>
<body>
<header><img class="logo" src="data:image/png;base64,{LOGO_B64_}" alt="truDigital"><h1>AI Support Agent</h1>{mode}
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
