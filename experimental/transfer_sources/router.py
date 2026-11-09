"""Unified transfer routing interface for Igor and TripPlan integration.

Stable callable interface:
- get_airport_transfer(origin_id, destination_id, travel_date, departure_time, mode_preference, live)
- Dispatches to TfL Journey Planner or MBTA Silver Line adapters.
- Validates parameters strictly.
- Never fabricates fares or silent fallbacks.
"""

from __future__ import annotations

from typing import Any

from .hongkong_adapter import HongKongTransferAdapter
from .mbta_adapter import (
    LOGAN_AIRPORT_STOPS,
    SOUTH_STATION_STOP_ID,
    MBTATransferAdapter,
)
from .milan_check import check_milan_malpensa_express_api_status
from .models import TransferError, TransferObservation
from .tfl_adapter import (
    HEATHROW_T5_RAIL_STOP_ID,
    PADDINGTON_RAIL_STOP_ID,
    TfLTransferAdapter,
)
from .transport import SafeHTTPClient

_tfl_client = TfLTransferAdapter()
_mbta_client = MBTATransferAdapter()
_hk_client = HongKongTransferAdapter()

# Known alias mappings for city/airport codes to exact stop IDs
ALIAS_TO_STOP_ID: dict[str, str] = {
    # London Heathrow
    "LHR": HEATHROW_T5_RAIL_STOP_ID,
    "HEATHROW_T5": HEATHROW_T5_RAIL_STOP_ID,
    "HEATHROW": HEATHROW_T5_RAIL_STOP_ID,
    "910GHTRWTM5": HEATHROW_T5_RAIL_STOP_ID,
    # London Paddington
    "PADDINGTON": PADDINGTON_RAIL_STOP_ID,
    "PAD": PADDINGTON_RAIL_STOP_ID,
    "910GPADTON": PADDINGTON_RAIL_STOP_ID,
    # Boston Logan
    "BOS": "17091",  # Default to Terminal A if generic BOS airport given
    "LOGAN": "17091",
    "LOGAN_TERMINAL_A": "17091",
    "17091": "17091",
    "27092": "27092",
    "17093": "17093",
    "17094": "17094",
    "17095": "17095",
    # Boston South Station
    "SOUTH_STATION": SOUTH_STATION_STOP_ID,
    "place-sstat": SOUTH_STATION_STOP_ID,
}


def get_airport_transfer(
    origin_id: str,
    destination_id: str,
    travel_date: str = "2026-11-22",
    departure_time: str = "11:00",
    mode_preference: str | None = None,
    live: bool = False,
    http_client: SafeHTTPClient | None = None,
) -> tuple[TransferObservation, ...]:
    """Unified entry point for airport transfer lookup.

    Resolves aliases to exact stop IDs and delegates to the appropriate adapter.
    """
    norm_from = ALIAS_TO_STOP_ID.get(origin_id.strip().upper(), origin_id.strip())
    norm_to = ALIAS_TO_STOP_ID.get(destination_id.strip().upper(), destination_id.strip())

    tfl_adapter = TfLTransferAdapter(http_client) if http_client else _tfl_client
    mbta_adapter = MBTATransferAdapter(http_client) if http_client else _mbta_client

    # 1. London Heathrow T5 -> London Paddington
    if norm_from == HEATHROW_T5_RAIL_STOP_ID and norm_to == PADDINGTON_RAIL_STOP_ID:
        return tfl_adapter.get_transfers(
            from_stop_id=norm_from,
            to_stop_id=norm_to,
            date_str=travel_date,
            time_str=departure_time,
            mode_preference=mode_preference,
            live=live,
        )

    # 2. Boston Logan Airport -> South Station
    if norm_from in LOGAN_AIRPORT_STOPS and norm_to == SOUTH_STATION_STOP_ID:
        return mbta_adapter.get_transfers(
            from_stop_id=norm_from,
            to_stop_id=norm_to,
            date_str=travel_date,
            direction_id=1,
            live=live,
        )

    # 3. Boston South Station -> Logan Airport (Reverse direction: NOT free)
    if norm_from == SOUTH_STATION_STOP_ID and norm_to in LOGAN_AIRPORT_STOPS:
        return mbta_adapter.get_transfers(
            from_stop_id=norm_from,
            to_stop_id=norm_to,
            date_str=travel_date,
            direction_id=0,
            live=live,
        )

    # 4. Milan Malpensa: Blocked
    if "MXP" in (norm_from, norm_to) or "MILAN" in (norm_from, norm_to):
        audit = check_milan_malpensa_express_api_status()
        raise TransferError(
            audit["blocker_code"],
            details=f"Malpensa Express integration is blocked: {audit['recommendation']}",
        )

    # 5. Unsupported corridor
    raise TransferError(
        "UNSUPPORTED_ROUTE",
        details=f"No supported airport transfer adapter for corridor {origin_id} -> {destination_id}",
    )
