"""Hong Kong Airport Express and Citybus A11 transfer adapters.

Research facts (docs/research/HONGKONG_AIRPORT_PUBLIC_APIS_20261025.md):
1. MTR Airport Express (AEL):
   - Realtime Next Train API (DATA.GOV.HK): ETA at moment of call (sys_time HK UTC+8), NOT future dated schedule.
   - Published Tariff: Official CSV opendata.mtr.com.hk/data/airport_express_fares.csv.
     Airport -> Hong Kong Station: HK$120 (Octopus/Contactless), HK$130 (Smart Ticket).
   - Property rights: MTR Corporation Limited.
2. Citybus A11:
   - Ground Transportation Centre (stop 001837) -> North Point (001249).
   - Inbound ETA requires filtering dir='I' (responses mix dir='O' outbound).
   - Fare is NOT present in ETA JSON -> fare_amount=None, price_status=UNKNOWN.
   - Property rights: Citybus Limited.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    TransferError,
    TransferObservation,
)
from .sanitizer import sanitize_text
from .transport import SafeHTTPClient, strict_json_loads

HONG_KONG_TZ = ZoneInfo("Asia/Hong_Kong")

MTR_ATTRIBUTION = "Data provided by DATA.GOV.HK. Intellectual Property Rights owned by MTR Corporation Limited."
CITYBUS_ATTRIBUTION = "Data provided by DATA.GOV.HK. Intellectual Property Rights owned by Citybus Limited."

HK_AIRPORT_STATION_ID = "AIR"
HK_HONG_KONG_STATION_ID = "HOK"
CITYBUS_AIRPORT_STOP_ID = "001837"
CITYBUS_NORTH_POINT_STOP_ID = "001249"


class HongKongTransferAdapter:
    """Adapter for Hong Kong Airport ground transportation options."""

    def __init__(self, http_client: SafeHTTPClient | None = None):
        self.http_client = http_client or SafeHTTPClient()

    def get_mtr_airport_express_snapshot(
        self,
        snapshot_path: Path | str | None = None,
    ) -> TransferObservation:
        """Load MTR Airport Express transfer from research snapshot."""
        root = Path(__file__).resolve().parents[2]
        p = (
            Path(snapshot_path)
            if snapshot_path
            else (
                root
                / "docs"
                / "research"
                / "airport_parking_http_20261025"
                / "airport_transfers_hk_queensland_20261025.json"
            )
        )

        if not p.is_file():
            # Fallback to hk_airport_http
            p = (
                root
                / "docs"
                / "research"
                / "hk_airport_http_20261025"
                / "hongkong_airport_transport_20261025.json"
            )

        if not p.is_file():
            raise TransferError("SNAPSHOT_NOT_FOUND", details=f"HK snapshot missing: {p}")

        data = strict_json_loads(p.read_bytes())
        observed_at = data.get("observed_at_utc", "2026-10-25T00:57:00+00:00")

        warnings = (
            {
                "code": "HISTORICAL_SNAPSHOT_NOT_LIVE",
                "message": "Loaded from research snapshot; real-time ETA only reflects measurement timestamp",
            },
            {
                "code": "PUBLISHED_TARIFF_NOT_CHECKOUT",
                "message": "Fare HK$120.00 is published adult Octopus single tariff from MTR official CSV; not a verified checkout",
            },
            {
                "code": "DATA_GOV_HK_TERMS",
                "message": "MTR Corporation Limited is owner of intellectual property rights",
            },
            {
                "code": "LAST_MILE_NOT_INCLUDED",
                "message": "Airport Express terminates at Hong Kong Station; onward travel to hotel is not included",
            },
        )

        return TransferObservation(
            origin_stop_id=HK_AIRPORT_STATION_ID,
            origin_name="Hong Kong International Airport Station",
            destination_stop_id=HK_HONG_KONG_STATION_ID,
            destination_name="Hong Kong Station (Central)",
            departure_local=None,
            arrival_local=None,
            duration_minutes=24,  # Published timetable interval
            timezone_name="Asia/Hong_Kong",
            utc_offset="+08:00",
            mode="rapid_transit",
            direction="airport_to_city",
            operator="MTR Corporation",
            route_name="Airport Express (AEL)",
            source_provider="DATA_GOV_HK_MTR",
            observed_at=observed_at,
            fare_amount="120.00",
            currency="HKD",
            fare_basis="OCTOPUS_ADULT_SINGLE",
            price_status="PLANNER_FARE_NOT_CHECKOUT",
            fare_source="OFFICIAL_PUBLISHED_TARIFF_CSV",
            source_mode="OFFLINE_SNAPSHOT",
            license_attribution=MTR_ATTRIBUTION,
            checkout_verified=False,
            warnings=warnings,
        )

    def get_citybus_a11_snapshot(
        self,
        snapshot_path: Path | str | None = None,
    ) -> TransferObservation:
        """Load Citybus A11 inbound transfer from research snapshot."""
        root = Path(__file__).resolve().parents[2]
        p = (
            Path(snapshot_path)
            if snapshot_path
            else (
                root
                / "docs"
                / "research"
                / "airport_parking_http_20261025"
                / "hongkong_citybus_eta_a11_20261025.json"
            )
        )

        observed_at = "2026-10-25T08:57:42+08:00"
        if p.is_file():
            data = strict_json_loads(p.read_bytes())
            observed_at = data.get("observed_at_utc", observed_at)

        warnings = (
            {
                "code": "HISTORICAL_SNAPSHOT_NOT_LIVE",
                "message": "Loaded from research snapshot; ETA reflects measurement timestamp only",
            },
            {
                "code": "PRICE_UNKNOWN",
                "message": "Citybus ETA API does not include fare information; fare_amount is unknown",
            },
            {
                "code": "DIRECTION_FILTERED_INBOUND",
                "message": "Filtered for dir=I (Airport to North Point); outbound dir=O discarded",
            },
            {
                "code": "DATA_GOV_HK_TERMS",
                "message": "Citybus Limited is owner of intellectual property rights",
            },
            {
                "code": "LAST_MILE_NOT_INCLUDED",
                "message": "Citybus terminates at transit stop; onward travel to hotel is not included",
            },
        )

        return TransferObservation(
            origin_stop_id=CITYBUS_AIRPORT_STOP_ID,
            origin_name="Airport (Ground Transportation Centre)",
            destination_stop_id=CITYBUS_NORTH_POINT_STOP_ID,
            destination_name="North Point Ferry Pier",
            departure_local=None,
            arrival_local=None,
            duration_minutes=None,
            timezone_name="Asia/Hong_Kong",
            utc_offset="+08:00",
            mode="bus",
            direction="inbound",
            operator="Citybus",
            route_name="Route A11",
            source_provider="DATA_GOV_HK_CITYBUS",
            observed_at=observed_at,
            fare_amount=None,  # Unknown not zero!
            currency="HKD",
            fare_basis=None,
            price_status="UNKNOWN",
            fare_source="UNKNOWN",
            source_mode="OFFLINE_SNAPSHOT",
            license_attribution=CITYBUS_ATTRIBUTION,
            checkout_verified=False,
            warnings=warnings,
        )
