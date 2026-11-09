"""MBTA Boston Logan Route 741 (Silver Line SL1) airport transfer adapter.

Connects Logan Airport Terminals to South Station (direction_id=1).
Applies direction-specific policy fare:
- Inbound (Airport -> South Station): Fare is $0.00 (POLICY_FREE) per Massport/MBTA policy.
- Outbound (South Station -> Airport): Refused or fare_amount=None (UNKNOWN), never zero!
- Timezone: America/New_York (handles EST vs EDT DST correctly).
- Separates scheduled departures from real-time ETA predictions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    SnapshotDateMismatchError,
    TransferError,
    TransferObservation,
)
from .sanitizer import sanitize_text
from .transport import SafeHTTPClient, strict_json_loads

MBTA_SCHEDULES_ENDPOINT = "https://api-v3.mbta.com/schedules"
MBTA_ATTRIBUTION = "Data provided by MBTA V3 API. Fare policy verified via Massport/MBTA official public guidelines."

LOGAN_AIRPORT_STOPS: dict[str, str] = {
    "17091": "Terminal A",
    "27092": "Terminal B Stop 1 - Arrivals Level",
    "17093": "Terminal B Stop 2 - Arrivals Level",
    "17094": "Terminal C - Arrivals Level",
    "17095": "Terminal E - Arrivals Level",
}

SOUTH_STATION_STOP_ID = "place-sstat"
SOUTH_STATION_NAME = "South Station"

NEW_YORK_TZ = ZoneInfo("America/New_York")


def _format_local_timestamp(iso_string: str | None, tz: ZoneInfo) -> tuple[str | None, str]:
    """Parse local timestamp and return ISO-8601 string with explicit offset and the offset string."""
    if not iso_string:
        return None, "-05:00"
    try:
        dt = datetime.fromisoformat(iso_string)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.astimezone(tz)
        z = dt.strftime("%z")
        offset_str = f"{z[:3]}:{z[3:]}" if len(z) == 5 else "-05:00"
        return dt.isoformat(), offset_str
    except Exception as exc:
        raise TransferError("MALFORMED_RESPONSE", details=f"Could not parse datetime: {iso_string}") from exc


class MBTATransferAdapter:
    """Adapter for MBTA V3 Silver Line SL1 airport transfers."""

    def __init__(self, http_client: SafeHTTPClient | None = None):
        self.http_client = http_client or SafeHTTPClient()

    def get_transfers(
        self,
        from_stop_id: str = "17091",
        to_stop_id: str = SOUTH_STATION_STOP_ID,
        date_str: str = "2026-11-22",
        direction_id: int = 1,
        live: bool = False,
        snapshot_path: Path | str | None = None,
    ) -> tuple[TransferObservation, ...]:
        """Fetch transfer options for MBTA SL1 (route 741).

        direction_id: 1 = Inbound (Airport -> South Station)
                      0 = Outbound (South Station -> Airport)
        """
        # Validate date
        clean_date = date_str.strip()
        if len(clean_date) == 8 and clean_date.isdigit():
            clean_date = f"{clean_date[:4]}-{clean_date[4:6]}-{clean_date[6:]}"

        try:
            datetime.strptime(clean_date, "%Y-%m-%d")
        except ValueError:
            raise TransferError("INVALID_INPUT", details=f"Invalid date format: {date_str}, expected YYYY-MM-DD")

        # Validate origin & destination compatibility
        is_inbound = (
            direction_id == 1
            and from_stop_id in LOGAN_AIRPORT_STOPS
            and to_stop_id == SOUTH_STATION_STOP_ID
        )
        is_outbound = (
            direction_id == 0
            and from_stop_id == SOUTH_STATION_STOP_ID
            and to_stop_id in LOGAN_AIRPORT_STOPS
        )

        if not is_inbound and not is_outbound:
            # Check if this is an unsupported route or stop
            if from_stop_id not in LOGAN_AIRPORT_STOPS and from_stop_id != SOUTH_STATION_STOP_ID:
                raise TransferError(
                    "UNSUPPORTED_ROUTE",
                    details=f"Unsupported origin stop {from_stop_id} for MBTA Route 741 SL1",
                )
            if to_stop_id not in LOGAN_AIRPORT_STOPS and to_stop_id != SOUTH_STATION_STOP_ID:
                raise TransferError(
                    "UNSUPPORTED_ROUTE",
                    details=f"Unsupported destination stop {to_stop_id} for MBTA Route 741 SL1",
                )
            # If direction mismatches origin/destination:
            raise TransferError(
                "UNSUPPORTED_ROUTE",
                details=f"Direction {direction_id} inconsistent with origin {from_stop_id} and destination {to_stop_id}",
            )

        if live:
            return self._fetch_live(
                from_stop_id=from_stop_id,
                to_stop_id=to_stop_id,
                date_str=clean_date,
                direction_id=direction_id,
                is_inbound=is_inbound,
            )
        else:
            return self._load_snapshot(
                from_stop_id=from_stop_id,
                to_stop_id=to_stop_id,
                date_str=clean_date,
                direction_id=direction_id,
                is_inbound=is_inbound,
                snapshot_path=snapshot_path,
            )

    def _fetch_live(
        self,
        from_stop_id: str,
        to_stop_id: str,
        date_str: str,
        direction_id: int,
        is_inbound: bool,
    ) -> tuple[TransferObservation, ...]:
        params = {
            "filter[route]": "741",
            "filter[stop]": from_stop_id,
            "filter[direction_id]": str(direction_id),
            "filter[date]": date_str,
            "page[limit]": "10",
        }

        status, data = self.http_client.get_json(MBTA_SCHEDULES_ENDPOINT, params=params)

        if not isinstance(data, dict):
            raise TransferError("MALFORMED_RESPONSE", details="Expected JSON:API object from MBTA V3")

        schedules = data.get("data")
        if not schedules or not isinstance(schedules, list):
            raise TransferError("EMPTY_SCHEDULE", details="No scheduled departures found for MBTA SL1 query")

        observed_at = datetime.now(timezone.utc).isoformat()
        return self._build_observations(
            schedules=schedules,
            from_stop_id=from_stop_id,
            to_stop_id=to_stop_id,
            direction_id=direction_id,
            is_inbound=is_inbound,
            source_mode="LIVE",
            observed_at=observed_at,
            requested_date=date_str,
        )

    def _load_snapshot(
        self,
        from_stop_id: str,
        to_stop_id: str,
        date_str: str,
        direction_id: int,
        is_inbound: bool,
        snapshot_path: Path | str | None,
    ) -> tuple[TransferObservation, ...]:
        root = Path(__file__).resolve().parents[2]
        if snapshot_path:
            p = Path(snapshot_path)
        else:
            p = (
                root
                / "docs"
                / "research"
                / "airport_parking_http_20261025"
                / "airport_parking_transit_v2_20261025.json"
            )

        if not p.is_file():
            raise TransferError("SNAPSHOT_NOT_FOUND", details=f"MBTA snapshot file missing: {p}")

        data = strict_json_loads(p.read_bytes())
        observed_at = data.get("observed_at_utc", "2026-10-25T00:48:31.859062+00:00")

        # Find mbta_sl1_airport_nov22 case
        cases = data.get("cases", [])
        mbta_case = next((c for c in cases if c.get("name") == "mbta_sl1_airport_nov22"), None)
        if not mbta_case:
            raise TransferError("SNAPSHOT_NOT_FOUND", details="Case mbta_sl1_airport_nov22 not found in fixture")

        samples = mbta_case.get("samples", [])
        if not samples:
            raise TransferError("EMPTY_SCHEDULE", details="No sample schedules in MBTA fixture")

        # Fail closed: check service date in snapshot matches requested date
        matching_samples = []
        available_dates = set()
        for s in samples:
            dep = s.get("departure_time")
            dep_local, _ = _format_local_timestamp(dep, NEW_YORK_TZ)
            if dep_local:
                s_date = dep_local.split("T")[0]
                available_dates.add(s_date)
                if s_date == date_str:
                    matching_samples.append(s)

        if not matching_samples:
            dates_str = ", ".join(sorted(available_dates)) if available_dates else "unknown"
            raise SnapshotDateMismatchError(
                f"Requested date {date_str} does not match MBTA snapshot service date ({dates_str})"
            )

        # Normalize sample format to match JSON:API attributes
        schedules = []
        for s in matching_samples:
            schedules.append(
                {
                    "attributes": {
                        "departure_time": s.get("departure_time"),
                        "arrival_time": s.get("arrival_time"),
                        "direction_id": direction_id,
                    },
                    "relationships": {
                        "trip": {"data": {"id": s.get("trip_id")}},
                        "route": {"data": {"id": "741"}},
                        "stop": {"data": {"id": from_stop_id}},
                    },
                }
            )

        return self._build_observations(
            schedules=schedules,
            from_stop_id=from_stop_id,
            to_stop_id=to_stop_id,
            direction_id=direction_id,
            is_inbound=is_inbound,
            source_mode="OFFLINE_SNAPSHOT",
            observed_at=observed_at,
            requested_date=date_str,
        )

    def _build_observations(
        self,
        schedules: list[dict[str, Any]],
        from_stop_id: str,
        to_stop_id: str,
        direction_id: int,
        is_inbound: bool,
        source_mode: str,
        observed_at: str,
        requested_date: str | None = None,
    ) -> tuple[TransferObservation, ...]:
        origin_name = LOGAN_AIRPORT_STOPS.get(from_stop_id, SOUTH_STATION_NAME)
        destination_name = LOGAN_AIRPORT_STOPS.get(to_stop_id, SOUTH_STATION_NAME)

        observations: list[TransferObservation] = []

        for item in schedules:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes", {})
            if not isinstance(attrs, dict):
                continue
            relationships = item.get("relationships", {})
            if not isinstance(relationships, dict):
                relationships = {}

            # 1. Validate direction_id if present in attributes
            ret_dir = attrs.get("direction_id")
            if ret_dir is not None:
                try:
                    if int(ret_dir) != direction_id:
                        # Mismatched direction returned! Filter out.
                        continue
                except (ValueError, TypeError):
                    continue

            # 2. Validate departure_time and service date
            dep_raw = attrs.get("departure_time")
            if not dep_raw:
                continue

            dep_local, utc_offset = _format_local_timestamp(dep_raw, NEW_YORK_TZ)
            if requested_date and dep_local:
                service_date = dep_local.split("T")[0]
                if service_date != requested_date:
                    # Mismatched departure date! Filter out.
                    continue

            # 3. Validate route if present in relationships or attributes
            route_rel = relationships.get("route", {})
            route_data = route_rel.get("data") if isinstance(route_rel, dict) else None
            route_id = route_data.get("id") if isinstance(route_data, dict) else attrs.get("route_id")
            if route_id is not None:
                if str(route_id) not in ("741", "SL1"):
                    # Mismatched route! Filter out.
                    continue

            # 4. Validate stop if present in relationships or attributes
            stop_rel = relationships.get("stop", {})
            stop_data = stop_rel.get("data") if isinstance(stop_rel, dict) else None
            stop_id = stop_data.get("id") if isinstance(stop_data, dict) else attrs.get("stop_id")
            if stop_id is not None:
                if str(stop_id) != str(from_stop_id):
                    # Mismatched stop! Filter out.
                    continue

            arr_raw = attrs.get("arrival_time")
            arr_local, _ = _format_local_timestamp(arr_raw, NEW_YORK_TZ) if arr_raw else (None, utc_offset)

            warnings: list[dict[str, str]] = []

            if source_mode == "OFFLINE_SNAPSHOT":
                warnings.append(
                    {
                        "code": "HISTORICAL_SNAPSHOT_NOT_LIVE",
                        "message": "Snapshot data from research fixtures, not a live query",
                    }
                )

            warnings.append(
                {
                    "code": "SCHEDULED_TIMETABLE_NOT_REALTIME_ETA",
                    "message": "Departure times reflect scheduled service; realtime predictions not valid for future dates",
                }
            )
            warnings.append(
                {
                    "code": "LAST_MILE_NOT_INCLUDED",
                    "message": "MBTA Silver Line SL1 terminates at Boston South Station; onward travel to hotel is not included",
                }
            )

            # Corridor validation for free policy:
            # Free inbound policy is strictly for Logan Airport stops -> South Station (direction_id 1)
            corridor_validated = (
                is_inbound
                and direction_id == 1
                and from_stop_id in LOGAN_AIRPORT_STOPS
                and to_stop_id == SOUTH_STATION_STOP_ID
                and (ret_dir is None or int(ret_dir) == 1)
                and (route_id is None or str(route_id) in ("741", "SL1"))
                and (stop_id is None or str(stop_id) == str(from_stop_id))
            )

            if corridor_validated:
                # Massport / MBTA official policy: free inbound
                fare_amount = "0.00"
                currency = "USD"
                fare_basis = "MASS_PORT_MBTA_FREE_INBOUND"
                price_status = "POLICY_FREE"
                fare_source = "OFFICIAL_DIRECTION_SPECIFIC_POLICY"
                warnings.append(
                    {
                        "code": "POLICY_DIRECTION_SPECIFIC",
                        "message": "Massport/MBTA policy provides free inbound service from Logan Airport to South Station",
                    }
                )
            else:
                # Outbound or unvalidated corridor: NOT free! Schedule has no fares.
                fare_amount = None
                currency = "USD"
                fare_basis = "UNKNOWN"
                price_status = "UNKNOWN"
                fare_source = "UNKNOWN"
                warnings.append(
                    {
                        "code": "PRICE_UNKNOWN",
                        "message": "Outbound travel towards Logan Airport requires regular MBTA fare; schedule API provides no fare data",
                    }
                )
                warnings.append(
                    {
                        "code": "REVERSE_DIRECTION_NOT_FREE_POLICY",
                        "message": "Massport free policy does not apply to outbound trips towards Logan Airport",
                    }
                )

            obs = TransferObservation(
                origin_stop_id=from_stop_id,
                origin_name=sanitize_text(origin_name),
                destination_stop_id=to_stop_id,
                destination_name=sanitize_text(destination_name),
                departure_local=dep_local,
                arrival_local=arr_local,
                duration_minutes=None,  # Not provided in single-stop schedule query without trip trace
                timezone_name="America/New_York",
                utc_offset=utc_offset,
                mode="bus",
                direction="inbound" if is_inbound else "outbound",
                operator="MBTA",
                route_name="Silver Line SL1 (Route 741)",
                source_provider="MBTA_V3",
                observed_at=observed_at,
                fare_amount=fare_amount,
                currency=currency,
                fare_basis=fare_basis,
                price_status=price_status,
                fare_source=fare_source,
                source_mode=source_mode,
                license_attribution=MBTA_ATTRIBUTION,
                checkout_verified=False,
                warnings=tuple(warnings),
            )
            observations.append(obs)
            if len(observations) >= 6:
                break

        if not observations:
            raise TransferError(
                "EMPTY_SCHEDULE",
                details=f"No matching departures found for stop {from_stop_id}, direction {direction_id}, on {requested_date}",
            )

        return tuple(observations)
