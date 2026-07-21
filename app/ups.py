"""UPS integration: rate shopping and label creation.

Auth: OAuth client-credentials with UPS_CLIENT_ID / UPS_CLIENT_SECRET
(app created at developer.ups.com, tied to UPS_ACCOUNT_NUMBER).

- shop_rates(prep): all available services with prices (negotiated when
  the account has them) for the shipment's pack plan.
- create_label(prep, service_code): buys the shipment on the UPS account
  and returns tracking number(s) + base64 GIF label(s). ALWAYS triggered
  by a human clicking the button — never automatic.
"""
from __future__ import annotations

import base64
import logging
import time

import httpx

from .config import settings

log = logging.getLogger("ups")

_token: tuple[float, str] | None = None

SERVICE_NAMES = {
    "01": "Next Day Air",
    "02": "2nd Day Air",
    "03": "Ground",
    "12": "3 Day Select",
    "13": "Next Day Air Saver",
    "14": "Next Day Air Early",
    "59": "2nd Day Air A.M.",
    "75": "Heavy Goods",
}


def enabled() -> bool:
    return bool(settings.ups_client_id and settings.ups_client_secret and settings.ups_account_number)


def _access_token() -> str:
    global _token
    now = time.time()
    if _token and now < _token[0] - 60:
        return _token[1]
    basic = base64.b64encode(
        f"{settings.ups_client_id}:{settings.ups_client_secret}".encode()
    ).decode()
    r = httpx.post(
        f"{settings.ups_api_url}/security/v1/oauth/token",
        data={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if r.status_code >= 400:
        log.error("UPS token -> %s: %s", r.status_code, r.text[:300])
    r.raise_for_status()
    data = r.json()
    _token = (now + int(data.get("expires_in", 3600)), data["access_token"])
    return _token[1]


class UPSError(Exception):
    """Carries UPS's human-readable error message."""


def _post(path: str, payload: dict) -> dict:
    r = httpx.post(
        f"{settings.ups_api_url}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"},
        timeout=60,
    )
    if r.status_code >= 400:
        log.error("UPS POST %s -> %s: %s", path, r.status_code, r.text[:800])
        try:
            errs = (r.json().get("response") or {}).get("errors") or []
            msg = "; ".join(e.get("message", "") for e in errs if e.get("message"))
        except Exception:
            msg = ""
        raise UPSError(msg or f"UPS returned HTTP {r.status_code}")
    return r.json()


def _shipper() -> dict:
    return {
        "Name": settings.ship_from_name[:35],
        "ShipperNumber": settings.ups_account_number,
        "Phone": {"Number": settings.ship_from_phone},
        "Address": {
            "AddressLine": [settings.ship_from_address1],
            "City": settings.ship_from_city,
            "StateProvinceCode": settings.ship_from_state,
            "PostalCode": settings.ship_from_zip,
            "CountryCode": "US",
        },
    }


def _ship_to(prep: dict) -> dict:
    a = prep.get("addr") or {}
    lines = [x for x in [a.get("line1"), a.get("line2")] if x]
    return {
        "Name": (prep.get("customer") or "Customer")[:35],
        "AttentionName": (a.get("attention") or prep.get("customer") or "")[:35],
        "Phone": {"Number": settings.ship_from_phone},  # fallback; UPS requires one
        "Address": {
            "AddressLine": lines or [""],
            "City": a.get("city") or "",
            "StateProvinceCode": a.get("state") or "",
            "PostalCode": a.get("zip") or "",
            "CountryCode": a.get("country") or "US",
        },
    }


def _packages(prep: dict, for_shipping: bool) -> list[dict]:
    key = "Packaging" if for_shipping else "PackagingType"
    out = []
    for b in prep.get("packing") or []:
        out.append(
            {
                key: {"Code": "02", "Description": "Box"},
                "Dimensions": {
                    "UnitOfMeasurement": {"Code": "IN"},
                    "Length": str(b["l"]),
                    "Width": str(b["w"]),
                    "Height": str(b["h"]),
                },
                "PackageWeight": {
                    "UnitOfMeasurement": {"Code": "LBS"},
                    "Weight": str(b["weight"]),
                },
            }
        )
    return out


def shop_rates(prep: dict) -> tuple[list[dict] | None, str]:
    """All service options with prices for this prep sheet.
    Returns (rates, error_message) — rates is None on failure."""
    if not enabled() or not prep.get("packing"):
        return None, "UPS is not configured or the order has no player boxes."
    payload = {
        "RateRequest": {
            "Request": {"RequestOption": "Shop"},
            "Shipment": {
                "Shipper": _shipper(),
                "ShipTo": _ship_to(prep),
                "ShipFrom": _shipper(),
                "ShipmentRatingOptions": {"NegotiatedRatesIndicator": ""},
                "Package": _packages(prep, for_shipping=False),
            },
        }
    }
    try:
        data = _post("/api/rating/v1/Shop", payload)
    except UPSError as e:
        return None, str(e)
    except Exception:
        log.exception("UPS Shop rates failed")
        return None, "Could not reach UPS — try again in a minute."
    rated = (data.get("RateResponse") or {}).get("RatedShipment") or []
    if isinstance(rated, dict):
        rated = [rated]
    out = []
    for rs in rated:
        code = (rs.get("Service") or {}).get("Code") or ""
        neg = ((rs.get("NegotiatedRateCharges") or {}).get("TotalCharge") or {}).get("MonetaryValue")
        pub = (rs.get("TotalCharges") or {}).get("MonetaryValue")
        price = neg or pub
        days = ((rs.get("GuaranteedDelivery") or {}).get("BusinessDaysInTransit")) or ""
        out.append(
            {
                "code": code,
                "name": SERVICE_NAMES.get(code, f"UPS service {code}"),
                "price": price,
                "negotiated": bool(neg),
                "days": days,
            }
        )
    out.sort(key=lambda x: float(x["price"] or 9e9))
    return out, ""


def create_label(prep: dict, service_code: str) -> tuple[dict | None, str]:
    """Create the shipment (bills the UPS account) and return tracking + labels.
    Returns (result, error_message) — result is None on failure."""
    if not enabled() or not prep.get("packing"):
        return None, "UPS is not configured or the order has no player boxes."
    payload = {
        "ShipmentRequest": {
            "Request": {"RequestOption": "nonvalidate"},
            "Shipment": {
                "Description": "Digital signage players",
                "Shipper": _shipper(),
                "ShipTo": _ship_to(prep),
                "ShipFrom": _shipper(),
                "PaymentInformation": {
                    "ShipmentCharge": {
                        "Type": "01",
                        "BillShipper": {"AccountNumber": settings.ups_account_number},
                    }
                },
                "Service": {"Code": service_code, "Description": SERVICE_NAMES.get(service_code, "")},
                "Package": _packages(prep, for_shipping=True),
                "ShipmentRatingOptions": {"NegotiatedRatesIndicator": ""},
            },
            "LabelSpecification": {
                "LabelImageFormat": {"Code": "GIF"},
                "HTTPUserAgent": "Mozilla/5.0",
            },
        }
    }
    try:
        data = _post("/api/shipments/v1/ship", payload)
    except UPSError as e:
        return None, str(e)
    except Exception:
        log.exception("UPS create shipment failed")
        return None, "Could not reach UPS — try again in a minute."
    results = (data.get("ShipmentResponse") or {}).get("ShipmentResults") or {}
    pkg = results.get("PackageResults") or []
    if isinstance(pkg, dict):
        pkg = [pkg]
    labels = []
    trackings = []
    for p in pkg:
        trackings.append(p.get("TrackingNumber") or "")
        img = ((p.get("ShippingLabel") or {}).get("GraphicImage")) or ""
        if img:
            labels.append(img)
    charge = (
        ((results.get("NegotiatedRateCharges") or {}).get("TotalCharge") or {}).get("MonetaryValue")
        or ((results.get("ShipmentCharges") or {}).get("TotalCharges") or {}).get("MonetaryValue")
        or ""
    )
    return {
        "shipment_id": results.get("ShipmentIdentificationNumber") or (trackings[0] if trackings else ""),
        "trackings": [t for t in trackings if t],
        "labels": labels,
        "charge": charge,
        "service": SERVICE_NAMES.get(service_code, service_code),
    }, ""
