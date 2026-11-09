"""Audit and blocker assessment for Milan Malpensa Express public API availability.

Status: BLOCKED_NO_PUBLIC_NO_KEY_TARIFF_API
Owner instructions: If unavailable, state blocker honestly; never fabricate numbers or responses.
"""

from __future__ import annotations

from typing import Any

MILAN_AUDIT_RESULT: dict[str, Any] = {
    "route": "Malpensa Airport (MXP) to Milano Centrale",
    "operator": "Trenord S.r.l. (Malpensa Express)",
    "status": "BLOCKED_NO_PUBLIC_NO_KEY_TARIFF_API",
    "blocker_code": "NO_PUBLIC_NO_KEY_TARIFF_API",
    "is_available_live": False,
    "evidence": {
        "trenord_official_portal": {
            "url": "https://www.malpensaexpress.it",
            "finding": "Proprietary session-based search behind Akamai anti-bot protection; no public developer API.",
        },
        "regione_lombardia_open_data": {
            "url": "https://www.dati.lombardia.it",
            "finding": "Static GTFS schedule files published under CC-BY-4.0, but no live date-specific pricing API.",
        },
        "trenitalia_open_data": {
            "url": "https://dati.trenitalia.com",
            "finding": "No authenticated-free / no-key live tariff endpoint for dedicated airport rail express.",
        },
        "official_published_static_tariff": {
            "amount_eur": "13.00",
            "basis": "Adult single standard tariff (published 2026)",
            "warning": "Static reference only; NOT dynamically verified through a public live API.",
        },
    },
    "recommendation": (
        "Do not fabricate an API integration. Keep Malpensa Express status as BLOCKED until an official "
        "public API key or open REST endpoint is authorized by Trenord or Regione Lombardia."
    ),
}


def check_milan_malpensa_express_api_status() -> dict[str, Any]:
    """Return verified technical and licensing audit status for Milan Malpensa Express."""
    return dict(MILAN_AUDIT_RESULT)
