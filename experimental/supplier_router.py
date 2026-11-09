"""Explicit read-only supplier routing with owner-enabled TourMind fallback."""

import os
from contextvars import ContextVar
from live_data import DataError

guest_nationality = ContextVar("traveltech_guest_nationality", default=None)


def active_supplier():
    forced = os.environ.get("SUPPLIER_PROVIDER", "").upper()
    if forced not in ("", "TOURMIND", "LITEAPI", "TREG", "OSTROVOK", "ETG"):
        raise DataError("SUPPLIER_PROVIDER_UNKNOWN")
    if forced in ("OSTROVOK", "ETG"):
        return "ETG"
    if forced:
        return forced
    # Prefer the primary sandbox supplier whenever its authorized key is
    # configured, including via ~/.traveltech-secrets/hotel_keys.json.
    # Nationality belongs to each request and must be supplied explicitly;
    # it must not determine which supplier the application selects.
    from supplier_liteapi import api_key

    return "LITEAPI" if api_key() else "TOURMIND"


def nationality():
    return guest_nationality.get() or os.environ.get("LITEAPI_GUEST_NATIONALITY")


def endpoint(kind):
    if kind in ("liteapi_catalog", "liteapi_rates", "liteapi_detail"):
        from supplier_liteapi import api_key, BASE

        key = api_key()
        if not key:
            raise DataError("LITEAPI_KEY_NOT_CONFIGURED")
        return BASE + (
            {
                "liteapi_catalog": "/data/hotels",
                "liteapi_rates": "/hotels/rates",
                "liteapi_detail": "/data/hotel",
            }[kind]
        ), {"X-API-Key": key}
    if kind == "treg_discovery":
        from supplier_treg import gateway_config

        base, key = gateway_config()
        if not key:
            raise DataError("TREG_KEY_NOT_CONFIGURED")
        return base + "/call/treg.hotel.recon", {"X-GW-Key": key}
    raise DataError("READONLY_ENDPOINT_REFUSED")


FALLBACK_CITIES = {("сочи", "RU"), ("sochi", "RU"), ("dubai", "AE"), ("istanbul", "TR")}


def fallback_supported(request):
    return (request["city"].casefold(), request["country"]) in FALLBACK_CITIES


def search(public, request, location=None):
    """Route a read-only search to the explicitly configured hotel supplier.

    No automatic production booking or silent vendor fallback is allowed.
    LiteAPI sandbox rates remain observations, not confirmed reservations.
    """
    provider = active_supplier()
    if provider == "TREG":
        from supplier_treg import search as lookup

        return lookup(public, request)
    if provider == "ETG":
        raise DataError("ETG_EXPLICIT_SANDBOX_AUTHORIZATION_REQUIRED")
    if provider != "LITEAPI":
        raise DataError("SUPPLIER_ROUTER_CONTRACT")
    from supplier_liteapi import search as lookup

    attempts = []
    primary = None
    failure = None
    try:
        primary = lookup(public, request)
        attempts.append(
            {
                "supplier": "LiteAPI",
                "status": primary["status"],
                "source_kind": primary.get("source_kind"),
            }
        )
        if primary.get("offers"):
            primary["supplier_attempts"] = attempts
            return primary
    except DataError as exc:
        failure = exc
        attempts.append({"supplier": "LiteAPI", "status": "ERROR", "error": str(exc)})
    if os.environ.get("SUPPLIER_FALLBACK", "").upper() != "TOURMIND" or not fallback_supported(
        request
    ):
        if failure:
            raise failure
        primary["supplier_attempts"] = attempts
        primary["fallback_unavailable_reason"] = "DISABLED_OR_UNSUPPORTED_CITY"
        return primary
    # A distinct, read-only provider query; no original POST is replayed.
    if location is None:
        from app import request_from
        from skill_system.backend import resolve_live_location

        resolution = public.location(request["city"])
        location, _ = resolve_live_location(request_from(request), resolution)
    try:
        result = public._search_tourmind(request, location)
    except DataError as exc:
        raise DataError("SUPPLIER_FALLBACK_FAILED:" + str(exc)) from exc
    result.update(
        supplier="TourMind",
        supplier_attempts=attempts + [{"supplier": "TourMind", "status": result["status"]}],
        fallback_used=True,
        fallback_reason=attempts[0].get("error") or attempts[0]["status"],
        primary_supplier="LiteAPI",
    )
    return result


def public_supplier_status():
    from supplier_liteapi import api_key

    provider = active_supplier()
    return {
        "active_supplier": provider,
        "booking_enabled": False,
        "liteapi_key_configured": bool(api_key()),
        "guest_nationality_configured": bool(nationality()),
        "guest_nationality_required": provider == "LITEAPI",
        "mode": "SANDBOX_NOT_LIVE_INVENTORY"
        if provider == "LITEAPI"
        and os.environ.get("LITEAPI_ENVIRONMENT", "sandbox") != "production"
        else "DISCOVERY_ONLY"
        if provider == "TREG"
        else "READ_ONLY_OBSERVATIONS",
        "live_search_enabled": provider in ("TOURMIND", "LITEAPI"),
        "price_availability_claim": "SEARCH_IS_NOT_FINAL_PRICE_OR_BOOKING",
        "automatic_supplier_fallback": provider == "LITEAPI"
        and os.environ.get("SUPPLIER_FALLBACK", "").upper() == "TOURMIND",
        "fallback_supplier": "TourMind"
        if os.environ.get("SUPPLIER_FALLBACK", "").upper() == "TOURMIND"
        else None,
        "fallback_cities": ["Sochi, RU", "Dubai, AE", "Istanbul, TR"],
    }
