"""Zoho Books client for the shipping queue.

Reads sales orders that still need to ship, and turns each into a
"shipment prep sheet" using truDigital's box rules:
  - each player box is 7.6 x 6 x 2 in and weighs 0.5 lb
  - shipping box tiers: up to 2 players -> 9x6x4, up to 6 -> 12x9x6,
    up to 14 -> 12x12x12, up to 20 -> 16x17x18 (larger orders split
    into full 20-player boxes plus the smallest box for the remainder)

Auth: Zoho OAuth "self client" — ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET /
ZOHO_REFRESH_TOKEN env vars. Access tokens are refreshed automatically.
Everything degrades gracefully when not configured.
"""
from __future__ import annotations

import logging
import math
import time

import httpx

from .config import settings

log = logging.getLogger("zoho")

ACCOUNTS_URL = "https://accounts.zoho.com/oauth/v2/token"
BOOKS_BASE = "https://www.zohoapis.com/books/v3"

_token: tuple[float, str] | None = None  # (expires_at, access_token)
_orders_cache: tuple[float, list] | None = None
CACHE_TTL = 300


def enabled() -> bool:
    return bool(
        settings.zoho_client_id and settings.zoho_client_secret and settings.zoho_refresh_token
    )


def _access_token() -> str:
    global _token
    now = time.time()
    if _token and now < _token[0] - 60:
        return _token[1]
    r = httpx.post(
        ACCOUNTS_URL,
        data={
            "refresh_token": settings.zoho_refresh_token,
            "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"Zoho token refresh failed: {data}")
    _token = (now + int(data.get("expires_in", 3600)), data["access_token"])
    return _token[1]


def _get(path: str, **params) -> dict:
    params["organization_id"] = settings.zoho_org_id
    r = httpx.get(
        f"{BOOKS_BASE}{path}",
        params=params,
        headers={"Authorization": f"Zoho-oauthtoken {_access_token()}"},
        timeout=30,
    )
    if r.status_code >= 400:
        log.error("Zoho GET %s -> %s: %s", path, r.status_code, r.text[:300])
    r.raise_for_status()
    return r.json()


def _post(path: str, payload: dict, **params) -> dict:
    params["organization_id"] = settings.zoho_org_id
    r = httpx.post(
        f"{BOOKS_BASE}{path}",
        json=payload,
        params=params,
        headers={"Authorization": f"Zoho-oauthtoken {_access_token()}"},
        timeout=30,
    )
    if r.status_code >= 400:
        log.error("Zoho POST %s -> %s: %s", path, r.status_code, r.text[:400])
    r.raise_for_status()
    return r.json()


def mark_shipped(salesorder_id: str, players_qty: int, tracking: str, service: str) -> tuple[bool, str]:
    """After a UPS label is created: create the package + shipment on the Zoho
    sales order (with the tracking number) so the order completes in Zoho.
    Allocates only the players actually shipped; when the last players go out,
    any other unpacked goods lines are included too. Best-effort: returns (ok, message)."""
    global _orders_cache
    try:
        detail = _get(f"/salesorders/{salesorder_id}").get("salesorder") or {}
        player_lines, other_lines = [], []
        for li in detail.get("line_items") or []:
            if li.get("line_item_type") != "goods":
                continue
            rem = max(int(li.get("quantity") or 0) - int(li.get("quantity_packed") or 0), 0)
            if rem <= 0:
                continue
            (player_lines if _is_player_item(li.get("name")) else other_lines).append((li, rem))

        lines = []
        alloc_left = players_qty
        for li, rem in player_lines:
            if alloc_left <= 0:
                break
            take = min(rem, alloc_left)
            lines.append({"so_line_item_id": li["line_item_id"], "quantity": take})
            alloc_left -= take
        players_left_after = sum(rem for _, rem in player_lines) - (players_qty - alloc_left)
        if players_left_after <= 0:  # final player shipment: include remaining goods too
            for li, rem in other_lines:
                lines.append({"so_line_item_id": li["line_item_id"], "quantity": rem})
        if not lines:
            return False, "No unpacked items left on the Zoho sales order."

        today = time.strftime("%Y-%m-%d")
        suffix = (tracking or "")[-8:] or str(int(time.time()))
        pkg_payload: dict = {"date": today, "line_items": lines}
        try:
            pkg = _post("/packages", pkg_payload, salesorder_id=salesorder_id)
        except httpx.HTTPStatusError:
            pkg_payload["package_number"] = f"PKG-{suffix}"
            pkg = _post("/packages", pkg_payload, salesorder_id=salesorder_id)
        package_id = (pkg.get("package") or {}).get("package_id")
        if not package_id:
            return False, "Zoho created no package id."

        ship_payload: dict = {
            "date": today,
            "delivery_method": service or "UPS",
            "tracking_number": tracking or "",
            "notes": "Created automatically by the truDigital support dashboard",
        }
        try:
            _post("/shipmentorders", ship_payload, salesorder_id=salesorder_id,
                  package_ids=package_id, send_notification="false")
        except httpx.HTTPStatusError:
            ship_payload["shipment_number"] = f"SHP-{suffix}"
            _post("/shipmentorders", ship_payload, salesorder_id=salesorder_id,
                  package_ids=package_id, send_notification="false")
        _orders_cache = None  # queue refresh reflects the new shipped status
        return True, "Package + shipment created in Zoho with tracking."
    except httpx.HTTPStatusError as e:
        try:
            msg = e.response.json().get("message", "")
        except Exception:
            msg = ""
        log.exception("Zoho mark_shipped failed")
        if e.response.status_code in (401, 403) or "not authorized" in msg.lower() or "scope" in msg.lower():
            return False, "Zoho token lacks write access — regenerate it with full access scope."
        return False, f"Zoho error: {msg or e.response.status_code}"
    except Exception as e:
        log.exception("Zoho mark_shipped failed")
        return False, str(e)[:200]


def _is_player_item(name: str) -> bool:
    return "player" in (name or "").lower()


US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


def state_code(addr: dict) -> str:
    """Best-effort 2-letter state code from a Zoho address."""
    sc = (addr.get("state_code") or "").strip()
    if len(sc) == 2:
        return sc.upper()
    name = (addr.get("state") or sc or "").strip().lower()
    return US_STATES.get(name, sc.upper()[:2])


# Shipping box tiers: (max players, (L, W, H) in inches)
BOX_TIERS = [
    (2, (9, 6, 4)),
    (6, (12, 9, 6)),
    (14, (12, 12, 12)),
    (20, (16, 17, 18)),
]


def _box(dims: tuple, players: int) -> dict:
    l, w, h = dims
    return {
        "l": l,
        "w": w,
        "h": h,
        "dims": f"{l} x {w} x {h} in",
        "players": players,
        "weight": round(max(players * settings.player_weight_lb, 0.5), 1),
    }


def pack_players(n: int) -> list[dict]:
    """Pack N players into shipping boxes: full 20-player boxes for bulk,
    then the smallest tier that fits the remainder."""
    out: list[dict] = []
    max_cap, max_dims = BOX_TIERS[-1]
    while n > max_cap:
        out.append(_box(max_dims, max_cap))
        n -= max_cap
    if n > 0:
        for cap, dims in BOX_TIERS:
            if n <= cap:
                out.append(_box(dims, n))
                break
    return out


def plan_lines(packing: list[dict]) -> list[str]:
    return [
        f"{b['dims']} — {b['players']} player{'s' if b['players'] != 1 else ''} ({b['weight']} lb)"
        for b in packing
    ]


def repack(prep: dict, qty: int) -> dict:
    """Copy of the prep sheet re-packed for a partial shipment of `qty` players
    (multi-address orders: ship some players now, the rest stay in the queue)."""
    total = prep.get("players") or 0
    qty = max(1, min(int(qty), total)) if total else 0
    out = dict(prep)
    packing = pack_players(qty)
    out["players"] = qty
    out["packing"] = packing
    out["boxes"] = len(packing)
    out["pack_plan"] = plan_lines(packing)
    out["weight_lb"] = round(qty * settings.player_weight_lb, 1)
    return out


def get_prep(salesorder_id: str) -> dict | None:
    """Fetch one sales order fresh and return its prep sheet."""
    if not enabled():
        return None
    try:
        detail = _get(f"/salesorders/{salesorder_id}").get("salesorder") or {}
    except Exception:
        log.exception("Zoho get_prep failed for %s", salesorder_id)
        return None
    cf = detail.get("custom_field_hash") or {}
    contact = (detail.get("contact_person_details") or [{}])
    summary = {
        "salesorder_id": detail.get("salesorder_id"),
        "salesorder_number": detail.get("salesorder_number"),
        "reference_number": detail.get("reference_number"),
        "customer_name": detail.get("customer_name"),
        "date": detail.get("date"),
        "created_time": detail.get("created_time", ""),
        "paid_status": detail.get("paid_status"),
        "cf_ready_to_ship": cf.get("cf_ready_to_ship", ""),
        "cf_ship_on_payment": cf.get("cf_ship_on_payment", ""),
        "email": contact[0].get("email", "") if contact else "",
    }
    return _prep(summary, detail)


def pending_shipments(force: bool = False) -> list[dict] | None:
    """Sales orders that still need to ship, newest first, with prep math.
    Returns None when Zoho isn't configured or unreachable."""
    global _orders_cache
    if not enabled():
        return None
    now = time.time()
    if _orders_cache and now - _orders_cache[0] < CACHE_TTL and not force:
        return _orders_cache[1]
    try:
        data = _get("/salesorders", per_page=100, sort_column="created_time", sort_order="D")
        orders = [
            o
            for o in data.get("salesorders") or []
            if o.get("shipped_status") == "pending"
            and o.get("status") not in ("void", "cancelled", "closed")
        ]
        out = []
        for o in orders:
            try:
                detail = _get(f"/salesorders/{o['salesorder_id']}").get("salesorder") or {}
            except Exception:
                detail = {}
            out.append(_prep(o, detail))
        _orders_cache = (now, out)
        return out
    except Exception:
        log.exception("Zoho pending_shipments failed")
        return None


def _prep(o: dict, detail: dict) -> dict:
    players = 0
    other_goods = []
    for li in detail.get("line_items") or []:
        qty = int(li.get("quantity") or 0)
        shipped = int(li.get("quantity_shipped") or 0)
        remaining = max(qty - shipped, 0)
        if remaining <= 0:
            continue
        if li.get("line_item_type") not in ("goods",):
            continue
        if _is_player_item(li.get("name")):
            players += remaining
        else:
            other_goods.append(f"{remaining}x {li.get('name')}")

    packing = pack_players(players)
    boxes = len(packing)
    pack_plan = [
        f"{b['dims']} — {b['players']} player{'s' if b['players'] != 1 else ''} ({b['weight']} lb)"
        for b in packing
    ]
    weight = round(players * settings.player_weight_lb, 1)
    addr = detail.get("shipping_address") or {}
    address = ", ".join(
        x
        for x in [
            addr.get("attention"),
            addr.get("address"),
            addr.get("street2"),
            addr.get("city"),
            f"{addr.get('state_code') or addr.get('state') or ''} {addr.get('zip') or ''}".strip(),
            addr.get("country_code"),
        ]
        if x
    )

    ref = o.get("reference_number") or ""
    rma_ticket = ""
    if "rma" in ref.lower():
        digits = "".join(ch for ch in ref if ch.isdigit())
        if len(digits) >= 6:
            rma_ticket = digits

    return {
        "salesorder_id": o.get("salesorder_id"),
        "number": o.get("salesorder_number"),
        "reference": ref,
        "rma_ticket": rma_ticket,
        "customer": o.get("customer_name"),
        "date": o.get("date"),
        "created_time": o.get("created_time", ""),
        "paid": o.get("paid_status") == "paid",
        "ready_to_ship": str(o.get("cf_ready_to_ship", "")).lower() == "true",
        "ship_on_payment": str(o.get("cf_ship_on_payment", "")).lower() == "true",
        "players": players,
        "other_goods": other_goods,
        "boxes": boxes,
        "packing": packing,
        "pack_plan": pack_plan,
        "weight_lb": weight,
        "address": address,
        "addr": {
            "attention": addr.get("attention") or "",
            "line1": addr.get("address") or "",
            "line2": addr.get("street2") or "",
            "city": addr.get("city") or "",
            "state": state_code(addr),
            "zip": addr.get("zip") or "",
            "country": addr.get("country_code") or "US",
        },
        "contact_email": o.get("email") or "",
    }
