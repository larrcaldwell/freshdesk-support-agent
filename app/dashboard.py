"""Team dashboard: GET /dashboard

Access: append ?key=YOUR_DASHBOARD_KEY once; a cookie keeps you signed in
after that, so the team can bookmark the plain /dashboard URL.
"""
from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from . import store, training
from .config import settings

log = logging.getLogger("dashboard")

router = APIRouter()

_new_tickets_cache: tuple[float, list] | None = None  # (fetched_at, tickets)


def _fresh_new_tickets(hours: int = 24) -> list | None:
    """Tickets created in Freshdesk in the last N hours (5-min cache). None on failure."""
    global _new_tickets_cache
    now = time.time()
    if _new_tickets_cache and now - _new_tickets_cache[0] < 300:
        tickets = _new_tickets_cache[1]
    else:
        try:
            from .freshdesk import fd

            tickets = fd._request("GET", "/tickets?per_page=100&order_by=created_at&order_type=desc") or []
            _new_tickets_cache = (now, tickets)
        except Exception:
            log.exception("Could not fetch new tickets from Freshdesk")
            return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for t in tickets:
        try:
            created = datetime.fromisoformat(str(t.get("created_at", "")).replace("Z", "+00:00"))
        except Exception:
            continue
        if created >= cutoff:
            out.append(t)
    return out

LOGO_B64 = "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"

ACTION_LABELS = {
    "chat-copilot": ("Chat suggestion", "#08974b", "#e5f9dc"),
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



def _teach_form(e: dict) -> str:
    subj = html.escape(e.get("subject") or "", quote=True)
    return (
        "<details class='teach'><summary>&#128172; Teach</summary>"
        "<form method='post' action='/feedback'>"
        f"<input type='hidden' name='ticket_id' value='{e.get('ticket_id') or 0}'>"
        f"<input type='hidden' name='subject' value='{subj}'>"
        "<textarea name='correction' required placeholder=\"What was wrong, or what should it have said? e.g. 'Never offer replacements before troubleshooting' or 'The user limit on Pro is 25'\"></textarea><br>"
        "<input type='text' name='author' placeholder='Your name (optional)'>"
        "<br><button type='submit'>Teach the agent</button>"
        "</form></details>"
    )


CHANNEL_LABELS = {
    "email": "&#9993; Email",
    "outbound-email": "&#9993; Email (outbound)",
    "portal": "&#127760; Portal",
    "chat": "&#128172; Chat",
    "live-chat": "&#128172; Live chat",
    "phone": "&#128222; Phone",
    "feedback-widget": "&#128221; Widget",
}


def _chat_link(ref: str) -> str:
    """Deep link into the Freshchat conversation for the agent team."""
    import os

    base = (settings.freshchat_api_url or "").replace("/v2", "").rstrip("/")
    acct = os.environ.get("FRESHCHAT_ACCOUNT_ID", "446218854017732")
    if base and ref:
        return f"{base}/a/{acct}/open/conversation/{ref}"
    return ""


def _event_row(e: dict, fd_url: str, teach: bool = True) -> str:
    conf = f"{e['confidence']}%" if e["confidence"] is not None else "–"
    human = "Yes" if e["needs_human"] else ("No" if e["needs_human"] is not None else "–")
    detail = html.escape(e["detail"] or "")
    channel = e.get("channel") or ""
    ch_label = CHANNEL_LABELS.get(channel, channel or "–")

    # Where does the row link? Live chats jump into the Freshchat conversation;
    # everything else opens the Freshdesk ticket.
    if channel == "live-chat" or (e["ticket_id"] == 0 and e.get("ref")):
        url = _chat_link(e.get("ref") or "")
        link = f"<a href='{url}' target='_blank'>Open chat &rarr;</a>" if url else "–"
    else:
        link = f"<a href='{fd_url}/{e['ticket_id']}' target='_blank'>#{e['ticket_id']}</a>"

    return (
        f"<tr><td data-ts='{e['ts']}'>{_fmt_time(e['ts'])}</td>"
        f"<td>{link}</td>"
        f"<td>{ch_label}</td>"
        f"<td class='subj'>{html.escape(e['subject'] or '')}</td>"
        f"<td>{html.escape(e['category'] or '–')}</td>"
        f"<td>{html.escape(e['sentiment'] or '–')}</td>"
        f"<td>{conf}</td><td>{human}</td><td>{_badge(e['action'])}"
        + (f"<div class='detail'>{detail}</div>" if e["action"] == "error" and detail else "")
        + (_teach_form(e) if teach else "")
        + "</td></tr>"
    )


EVENT_HEADERS = (
    "<tr><th>When</th><th>Ticket / Chat</th><th>Channel</th><th>Subject</th>"
    "<th>Category</th><th>Sentiment</th><th>Confidence</th><th>Needs human</th><th>Result</th></tr>"
)


SORT_JS = """<script>
document.querySelectorAll('table.sortable th').forEach(function(th, idx){
  th.style.cursor = 'pointer';
  th.title = 'Click to sort';
  th.addEventListener('click', function(){
    var table = th.closest('table');
    var rows = Array.from(table.querySelectorAll('tr')).slice(1).filter(r => r.cells.length > 1);
    var dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
    table.querySelectorAll('th').forEach(h => { h.dataset.dir=''; h.innerHTML=h.innerHTML.replace(/ [\\u25B2\\u25BC]$/,''); });
    th.dataset.dir = dir;
    th.innerHTML += dir === 'asc' ? ' \\u25B2' : ' \\u25BC';
    function key(r){
      var c = r.cells[idx];
      if (!c) return '';
      if (c.dataset.ts) return parseFloat(c.dataset.ts);
      var t = c.textContent.trim();
      var n = parseFloat(t.replace('%','').replace('#',''));
      return isNaN(n) ? t.toLowerCase() : n;
    }
    rows.sort(function(a,b){
      var x = key(a), y = key(b);
      if (typeof x === 'number' && typeof y === 'number') return dir==='asc' ? x-y : y-x;
      return dir==='asc' ? String(x).localeCompare(String(y)) : String(y).localeCompare(String(x));
    });
    rows.forEach(r => table.appendChild(r));
  });
});
</script>"""


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
    try:
        teachings = len(training.load_corrections())
    except Exception:
        teachings = "–"
    fd_url = f"https://{settings.freshdesk_domain}.freshdesk.com/a/tickets"

    rows = [_event_row(e, fd_url) for e in events]
    table = "".join(rows) or "<tr><td colspan='9' class='empty'>No activity yet. New tickets will appear here automatically.</td></tr>"

    new_tickets = _fresh_new_tickets(24)
    new_count = len(new_tickets) if new_tickets is not None else "–"

    mode = (
        "<span class='badge' style='color:#c0392b;background:#fdeaea'>AUTO-REPLY ON</span>"
        if settings.auto_reply_enabled
        else "<span class='badge' style='color:#08974b;background:#e5f9dc'>Draft mode — humans send every reply</span>"
    )

    def stat(label: str, value, href: str = "") -> str:
        if href:
            return f"<a class='card cardlink2' href='{href}'><div class='num'>{value}</div><div class='lbl'>{label} &rarr;</div></a>"
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
 a.cardlink {{ text-decoration:none; display:block; border:1px solid #5cdd31; }}
 a.cardlink:hover {{ background:#f4faf1; text-decoration:none; }}
 a.cardlink2 {{ text-decoration:none; display:block; }}
 a.cardlink2:hover {{ background:#f4faf1; text-decoration:none; box-shadow:0 2px 6px rgba(32,52,76,.18); }}
 .titem {{ background:#fff; border-radius:12px; padding:14px 18px; margin-bottom:10px; box-shadow:0 1px 3px rgba(32,52,76,.10); font-size:14px; }}
 .titem .n {{ color:#08974b; font-weight:700; margin-right:8px; }}
 details.teach {{ margin-top:6px; }}
 details.teach summary {{ cursor:pointer; color:#08974b; font-size:12px; font-weight:600; list-style:none; }}
 details.teach summary:hover {{ text-decoration:underline; }}
 details.teach form {{ margin-top:8px; background:#f4faf1; border:1px solid #d7f0c9; border-radius:10px; padding:10px; }}
 details.teach textarea {{ width:100%; box-sizing:border-box; min-height:64px; border:1px solid #cfd8d4; border-radius:8px; padding:8px; font-family:inherit; font-size:13px; }}
 details.teach input[type=text] {{ border:1px solid #cfd8d4; border-radius:8px; padding:6px 8px; font-family:inherit; font-size:12.5px; margin-top:6px; }}
 details.teach button {{ background:#08974b; color:#fff; border:none; border-radius:8px; padding:7px 14px; font-weight:600; font-size:12.5px; margin-top:8px; cursor:pointer; }}
 details.teach button:hover {{ background:#067a3d; }}
 footer {{ color:#75808e; font-size:12px; margin:18px 4px; }}
</style></head>
<body>
<header><img class="logo" src="data:image/png;base64,{LOGO_B64_}" alt="truDigital"><h1>AI Support Agent</h1>{mode}
 <span class="sub">Model: {html.escape(settings.model)} · Confidence bar: {settings.auto_reply_min_confidence}% · Page refreshes every 60s</span>
</header>
<div class="wrap">
 <div class="cards">
  {stat("New tickets · 24h", new_count, "/activity?f=new")}
  {stat("Handled · 24h", day.get("total", 0), "/activity?f=all&r=24h")}
  {stat("Drafts ready · 24h", day.get("draft-posted", 0), "/activity?f=draft-posted&r=24h")}
  {stat("Auto-replied · 24h", day.get("auto-replied", 0), "/activity?f=auto-replied&r=24h")}
  {stat("Errors · 24h", day.get("error", 0), "/activity?f=error&r=24h")}
  {stat("Handled · 7 days", week.get("total", 0), "/activity?f=all&r=7d")}
  <a class='card cardlink' href='/teachings'><div class='num'>{teachings}</div><div class='lbl'>Team teachings →</div></a>
 </div>
 <table class="sortable">
  {EVENT_HEADERS}
  {table}
 </table>
 <footer><a href='/export.zip'>&#11015; Export all data (.zip)</a> — reasoning journal + activity log + teachings in one bundle, for audits or model training. (Journal alone: <a href='/journal.jsonl'>.jsonl</a>)<br>Click a ticket number to open it in Freshdesk — the agent's triage note and draft reply are in the ticket as a private note.
 "Needs human: Yes" = the agent wants one of you to review before anything goes out.</footer>
</div>
{SORT_JS}
</body></html>"""

    resp = HTMLResponse(body)
    if request.query_params.get("key") == settings.dashboard_key:
        resp.set_cookie("fd_agent_key", settings.dashboard_key, max_age=60 * 60 * 24 * 90, httponly=True)
    return resp


FILTERS = {
    "all": ("All handled", None),
    "draft-posted": ("Drafts", "draft-posted"),
    "auto-replied": ("Auto-replied", "auto-replied"),
    "triage-only": ("Triaged only", "triage-only"),
    "error": ("Errors", "error"),
    "needs-human": ("Needs human", None),
    "chat-copilot": ("Live chats", "chat-copilot"),
    "new": ("New tickets (Freshdesk)", None),
}
RANGES = {"24h": ("Last 24h", 24), "7d": ("Last 7 days", 24 * 7), "30d": ("Last 30 days", 24 * 30), "all": ("All time", None)}
FD_STATUS = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
FD_PRIORITY = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}


@router.get("/activity", response_class=HTMLResponse)
def activity(request: Request) -> HTMLResponse:
    """Drill-down view: filter, search, and sort everything the agent has done —
    plus a live 'new tickets' view straight from Freshdesk."""
    if not settings.dashboard_key:
        return HTMLResponse("<h3>Dashboard disabled.</h3>", status_code=503)
    supplied = request.query_params.get("key") or request.cookies.get("fd_agent_key")
    if supplied != settings.dashboard_key:
        return HTMLResponse("<h3 style='font-family:sans-serif'>Access key required.</h3>", status_code=401)

    f = request.query_params.get("f", "all")
    r = request.query_params.get("r", "7d")
    q = (request.query_params.get("q") or "").strip().lower()
    if f not in FILTERS:
        f = "all"
    if r not in RANGES:
        r = "7d"
    fd_url = f"https://{settings.freshdesk_domain}.freshdesk.com/a/tickets"

    if f == "new":
        tickets = _fresh_new_tickets(RANGES[r][1] or 24 * 365) or []
        if q:
            tickets = [t for t in tickets if q in (t.get("subject") or "").lower()]
        headers = "<tr><th>Created</th><th>Ticket</th><th>Subject</th><th>Status</th><th>Priority</th></tr>"
        rows = []
        for t in tickets:
            try:
                ts = datetime.fromisoformat(str(t.get("created_at", "")).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0
            rows.append(
                f"<tr><td data-ts='{ts}'>{_fmt_time(ts)}</td>"
                f"<td><a href='{fd_url}/{t.get('id')}' target='_blank'>#{t.get('id')}</a></td>"
                f"<td class='subj'>{html.escape(t.get('subject') or '')}</td>"
                f"<td>{FD_STATUS.get(t.get('status'), t.get('status'))}</td>"
                f"<td>{FD_PRIORITY.get(t.get('priority'), t.get('priority'))}</td></tr>"
            )
        table = "".join(rows) or "<tr><td colspan='5' class='empty'>No new tickets in this window (or Freshdesk unreachable).</td></tr>"
        count = len(tickets)
    else:
        events = store.events_since(RANGES[r][1])
        if f == "needs-human":
            events = [e for e in events if e.get("needs_human")]
        elif FILTERS[f][1]:
            events = [e for e in events if e.get("action") == FILTERS[f][1]]
        if q:
            events = [
                e for e in events
                if q in (e.get("subject") or "").lower() or q in (e.get("category") or "").lower()
            ]
        headers = EVENT_HEADERS
        rows = [_event_row(e, fd_url) for e in events]
        table = "".join(rows) or "<tr><td colspan='9' class='empty'>Nothing matches this filter.</td></tr>"
        count = len(rows)

    def pill(key: str, label: str, current: str, param: str) -> str:
        qs = f"f={f if param == 'r' else key}&r={key if param == 'r' else r}"
        cls = "pill on" if key == current else "pill"
        return f"<a class='{cls}' href='/activity?{qs}'>{label}</a>"

    filter_pills = "".join(pill(k, v[0], f, "f") for k, v in FILTERS.items())
    range_pills = "".join(pill(k, v[0], r, "r") for k, v in RANGES.items())

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Activity — truDigital AI Support Agent</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
<style>
 body {{ font-family:'Poppins',sans-serif; margin:0; background:#ebeef2; color:#20344c; }}
 header {{ background:#fff; padding:14px 28px; display:flex; align-items:center; gap:16px; border-bottom:4px solid #5cdd31; flex-wrap:wrap; }}
 header img {{ height:34px; }} header h1 {{ font-size:17px; margin:0; font-weight:600; }}
 .wrap {{ max-width:1200px; margin:22px auto; padding:0 16px; }}
 .bar {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:14px; }}
 .pill {{ background:#fff; border-radius:20px; padding:6px 14px; font-size:12.5px; font-weight:600; color:#20344c; text-decoration:none; box-shadow:0 1px 3px rgba(32,52,76,.10); }}
 .pill.on {{ background:#08974b; color:#fff; }}
 .pill:hover {{ background:#5cdd31; color:#20344c; }}
 .search {{ margin-left:auto; }}
 .search input {{ border:1px solid #cfd8d4; border-radius:20px; padding:7px 14px; font-family:inherit; font-size:13px; width:220px; }}
 table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:14px; overflow:hidden; box-shadow:0 1px 3px rgba(32,52,76,.10); }}
 th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:#75808e; padding:10px 12px; border-bottom:2px solid #ebeef2; font-weight:600; user-select:none; }}
 td {{ padding:10px 12px; border-bottom:1px solid #ebeef2; font-size:13.5px; vertical-align:top; }}
 tr:hover td {{ background:#f4faf1; }}
 a {{ color:#08974b; font-weight:600; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
 .badge {{ padding:3px 9px; border-radius:20px; font-size:12px; font-weight:600; white-space:nowrap; }}
 .subj {{ max-width:340px; }}
 .detail {{ color:#c0392b; font-size:12px; margin-top:4px; }}
 .empty {{ text-align:center; color:#75808e; padding:28px; }}
 .meta {{ color:#75808e; font-size:12.5px; margin:12px 2px; }}
 details.teach {{ margin-top:6px; }}
 details.teach summary {{ cursor:pointer; color:#08974b; font-size:12px; font-weight:600; list-style:none; }}
 details.teach form {{ margin-top:8px; background:#f4faf1; border:1px solid #d7f0c9; border-radius:10px; padding:10px; }}
 details.teach textarea {{ width:100%; box-sizing:border-box; min-height:64px; border:1px solid #cfd8d4; border-radius:8px; padding:8px; font-family:inherit; font-size:13px; }}
 details.teach input[type=text] {{ border:1px solid #cfd8d4; border-radius:8px; padding:6px 8px; font-family:inherit; font-size:12.5px; margin-top:6px; }}
 details.teach button {{ background:#08974b; color:#fff; border:none; border-radius:8px; padding:7px 14px; font-weight:600; font-size:12.5px; margin-top:8px; cursor:pointer; }}
</style></head><body>
<header><img src="data:image/png;base64,{LOGO_B64}" alt="truDigital"><h1>Activity</h1>
<span style="color:#75808e;font-size:12.5px">{count} row{'s' if count != 1 else ''} · {FILTERS[f][0]} · {RANGES[r][0]} · click any column header to sort</span></header>
<div class="wrap">
 <div class="meta"><a href="/dashboard">&larr; Back to dashboard</a></div>
 <div class="bar">{filter_pills}</div>
 <div class="bar">{range_pills}
  <form class="search" method="get" action="/activity">
   <input type="hidden" name="f" value="{f}"><input type="hidden" name="r" value="{r}">
   <input type="text" name="q" value="{html.escape(q, quote=True)}" placeholder="Search subject or category…">
  </form>
 </div>
 <table class="sortable">
  {headers}
  {table}
 </table>
</div>
{SORT_JS}
</body></html>"""
    return HTMLResponse(body)


@router.get("/teachings", response_class=HTMLResponse)
def teachings_page(request: Request) -> HTMLResponse:
    if not settings.dashboard_key:
        return HTMLResponse("<h3>Dashboard disabled.</h3>", status_code=503)
    supplied = request.query_params.get("key") or request.cookies.get("fd_agent_key")
    if supplied != settings.dashboard_key:
        return HTMLResponse("<h3 style='font-family:sans-serif'>Access key required.</h3>", status_code=401)

    from . import training as _training

    try:
        items = _training.load_corrections()
    except Exception:
        items = []
    try:
        tid = _training._find_or_create_ticket()
        fd_link = f"https://{settings.freshdesk_domain}.freshdesk.com/a/tickets/{tid}"
    except Exception:
        fd_link = "#"

    rows = "".join(
        f"<div class='titem'><span class='n'>{i}.</span>{html.escape(c)}</div>"
        for i, c in enumerate(items, 1)
    ) or "<div class='titem'>Nothing taught yet — use the &#128172; Teach link on any dashboard row.</div>"

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Team teachings — truDigital AI Support Agent</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
<style>
 body {{ font-family:'Poppins',sans-serif; margin:0; background:#ebeef2; color:#20344c; }}
 header {{ background:#fff; padding:14px 28px; display:flex; align-items:center; gap:16px; border-bottom:4px solid #5cdd31; }}
 header img {{ height:34px; }} header h1 {{ font-size:17px; margin:0; font-weight:600; }}
 .wrap {{ max-width:900px; margin:22px auto; padding:0 16px; }}
 .titem {{ background:#fff; border-radius:12px; padding:14px 18px; margin-bottom:10px; box-shadow:0 1px 3px rgba(32,52,76,.10); font-size:14px; }}
 .titem .n {{ color:#08974b; font-weight:700; margin-right:8px; }}
 a {{ color:#08974b; font-weight:600; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
 .meta {{ color:#75808e; font-size:12.5px; margin:14px 2px; }}
</style></head><body>
<header><img src="data:image/png;base64,{LOGO_B64}" alt="truDigital"><h1>Team teachings</h1>
<span style="color:#75808e;font-size:12.5px">{len(items)} rule{'s' if len(items) != 1 else ''} the agent follows on every draft — newest first</span></header>
<div class="wrap">
 <div class="meta"><a href="/dashboard">&larr; Back to dashboard</a> &nbsp;·&nbsp;
 <a href="{fd_link}" target="_blank">Edit in Freshdesk (AI Training Log ticket)</a> — add a [teach] note there or use the Teach button on the dashboard.</div>
 {rows}
</div></body></html>"""
    return HTMLResponse(body)
