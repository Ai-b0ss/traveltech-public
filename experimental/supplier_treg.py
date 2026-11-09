"""Discovery is not a hotel rate. No generated hotels, prices or fallback data."""

import copy, os
from pathlib import Path
from urllib.parse import urlparse
from live_data import DataError


def gateway_config():
    base = os.environ.get("TREG_GW_URL", "http://127.0.0.1:8751").rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
        or parsed.path
        or parsed.query
        or parsed.username
    ):
        raise DataError("TREG_LOOPBACK_GATEWAY_REQUIRED")
    key = os.environ.get("TREG_GW_KEY")
    path = Path(
        os.environ.get(
            "TREG_GW_KEY_FILE",
            str(Path.home() / "Documents/Projects/treg-autoreg/dashboard/gw_key.txt"),
        )
    )
    if not key and path.exists():
        key = path.read_text(encoding="utf-8").strip()
    return base, key


def normalize(payload, request, stamp):
    hotels = (payload.get("data") or {}).get("hotels") if isinstance(payload, dict) else None
    if not isinstance(hotels, list) or len(hotels) > 100:
        raise DataError("SUPPLIER_DISCOVERY_SCHEMA")
    # The current gateway can return generated fixtures with HTTP 200. Its shape
    # cannot establish price, inventory, occupancy or hotel existence.
    candidates = [
        {
            "name": h.get("name"),
            "discovery_id": h.get("id", h.get("hid")),
            "identity_verified": False,
        }
        for h in hotels
        if isinstance(h, dict)
    ]
    return {
        "source": "TREG_DISCOVERY_UNVERIFIED",
        "source_kind": "DISCOVERY",
        "request": copy.deepcopy(request),
        "observed_at": stamp,
        "status": "DISCOVERY_ONLY",
        "offers": [],
        "candidates": candidates,
        "candidate_count": len(candidates),
        "availability_verified": False,
        "booking_performed": False,
        "warning": "Разведка не подтверждает существование отеля, цену, налоги или наличие. Тарифы не созданы.",
    }


def search(public, request):
    rec = public.call(
        "treg_discovery",
        params={
            "q": f"hotel {request['city']} {request['country']} {request['checkin']} {request['checkout']}",
            "city": request["city"],
        },
    )
    return normalize(rec["data"], request, rec["observed_at"])
