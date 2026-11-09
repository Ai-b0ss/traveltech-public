"""TfL Journey Planner airport transfer adapter.

Connects Heathrow Terminal 5 Rail Station (910GHTRWTM5) to London Paddington Rail Station (910GPADTON).
Supports Elizabeth Line and Heathrow Express (National Rail).
Converts fare totalCost from pence (minor unit) to GBP Decimal string without float loss.
Handles HTTP 300 disambiguation, empty journeys, missing fares, and timezone offsets (Europe/London).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    SnapshotDateMismatchError,
    TransferDisambiguationError,
    TransferError,
    TransferObservation,
)
from .sanitizer import sanitize_text
from .transport import SafeHTTPClient, strict_json_loads

TFL_JOURNEY_ENDPOINT = "https://api.tfl.gov.uk/Journey/JourneyResults/{from_stop}/to/{to_stop}"
TFL_ATTRIBUTION = (
    "Powered by TfL Open Data. Contains OS data (c) Crown copyright and database rights 2016 "
    "and Geomni UK Map data (c) and database rights [2019]"
)

HEATHROW_T5_RAIL_STOP_ID = "910GHTRWTM5"
PADDINGTON_RAIL_STOP_ID = "910GPADTON"
HEATHROW_T5_NAME = "Heathrow Terminal 5 Rail Station"
PADDINGTON_NAME = "London Paddington Rail Station"

LONDON_TZ = ZoneInfo("Europe/London")


def pence_to_gbp(pence: Any) -> str | None:
    """Convert minor currency unit (pence) to GBP Decimal string.

    Accepts only non-negative integer number of pence (int, str of digits, or integral Decimal).
    Rejects fractional pence ('12.5', '10.001'), non-finite ('NaN', 'Infinity'), bool, and negative numbers.
    1460 pence -> '14.60'
    2600 pence -> '26.00'
    0 pence -> '0.00'
    None -> None
    """
    if pence is None:
        return None

    if isinstance(pence, bool):
        raise TransferError("INVALID_FARE", details=f"Boolean not allowed for pence: {pence}")

    int_pence: int
    if isinstance(pence, int):
        if pence < 0:
            raise TransferError("INVALID_FARE", details=f"Negative fare value rejected: {pence}")
        int_pence = pence
    elif isinstance(pence, str):
        cleaned = pence.strip()
        if not cleaned.isdigit():
            raise TransferError("INVALID_FARE", details=f"Invalid pence fare value: {pence}")
        int_pence = int(cleaned)
    elif isinstance(pence, Decimal):
        if not pence.is_finite() or pence < 0 or pence != pence.to_integral_value():
            raise TransferError("INVALID_FARE", details=f"Invalid Decimal pence value: {pence}")
        int_pence = int(pence)
    else:
        raise TransferError("INVALID_FARE", details=f"Invalid pence fare type/value: {pence}")

    dec = Decimal(int_pence) / Decimal("100")
    return f"{dec:.2f}"


def _format_local_timestamp(iso_string: str | None, tz: ZoneInfo) -> tuple[str | None, str]:
    """Parse local timestamp and return ISO-8601 string with explicit offset and the offset string."""
    if not iso_string:
        return None, "+00:00"
    try:
        # TfL formats are typically '2026-11-22T11:12:00' without timezone
        dt = datetime.fromisoformat(iso_string)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.astimezone(tz)
        z = dt.strftime("%z")
        offset_str = f"{z[:3]}:{z[3:]}" if len(z) == 5 else "+00:00"
        return dt.isoformat(), offset_str
    except Exception as exc:
        raise TransferError("MALFORMED_RESPONSE", details=f"Could not parse datetime: {iso_string}") from exc


class TfLTransferAdapter:
    """Adapter for official TfL Journey Planner API."""

    def __init__(self, http_client: SafeHTTPClient | None = None):
        self.http_client = http_client or SafeHTTPClient()

    def get_transfers(
        self,
        from_stop_id: str = HEATHROW_T5_RAIL_STOP_ID,
        to_stop_id: str = PADDINGTON_RAIL_STOP_ID,
        date_str: str = "20261122",
        time_str: str = "1100",
        mode_preference: str | None = None,
        live: bool = False,
        snapshot_path: Path | str | None = None,
    ) -> tuple[TransferObservation, ...]:
        """Fetch transfer options between Heathrow and Paddington.

        date_str: YYYYMMDD or YYYY-MM-DD
        time_str: HHMM or HH:MM
        mode_preference: 'elizabeth-line', 'national-rail', or None for all
        live: If True, execute live HTTP GET (bounded, zero retry); if False, use offline snapshot.
        """
        # Normalize date and time
        clean_date = date_str.replace("-", "").strip()
        clean_time = time_str.replace(":", "").strip()

        if len(clean_date) != 8 or not clean_date.isdigit():
            raise TransferError("INVALID_INPUT", details=f"Invalid date format: {date_str}")
        if len(clean_time) != 4 or not clean_time.isdigit():
            raise TransferError("INVALID_INPUT", details=f"Invalid time format: {time_str}")

        requested_date = f"{clean_date[:4]}-{clean_date[4:6]}-{clean_date[6:]}"

        if live:
            return self._fetch_live(
                from_stop_id=from_stop_id,
                to_stop_id=to_stop_id,
                date_str=clean_date,
                time_str=clean_time,
                mode=mode_preference,
                requested_date=requested_date,
            )
        else:
            return self._load_snapshot(
                from_stop_id=from_stop_id,
                to_stop_id=to_stop_id,
                requested_date=requested_date,
                mode_preference=mode_preference,
                snapshot_path=snapshot_path,
            )

    def _fetch_live(
        self,
        from_stop_id: str,
        to_stop_id: str,
        date_str: str,
        time_str: str,
        mode: str | None,
        requested_date: str,
    ) -> tuple[TransferObservation, ...]:
        url = TFL_JOURNEY_ENDPOINT.format(from_stop=from_stop_id, to_stop=to_stop_id)
        params: dict[str, str] = {
            "date": date_str,
            "time": time_str,
            "timeIs": "Departing",
        }
        if mode:
            params["mode"] = mode
        else:
            params["journeyPreference"] = "leasttime"

        status, data = self.http_client.get_json(url, params=params)

        if status == 300:
            choices = self._extract_disambiguation_choices(data)
            raise TransferDisambiguationError(
                "TfL returned HTTP 300 Disambiguation. Exact stop IDs required.", choices=choices
            )

        if not isinstance(data, dict):
            raise TransferError("MALFORMED_RESPONSE", details="Expected JSON object from TfL Journey Planner")

        observed_at = datetime.now(timezone.utc).isoformat()
        return self._parse_journey_payload(
            data,
            from_stop_id=from_stop_id,
            to_stop_id=to_stop_id,
            requested_date=requested_date,
            source_mode="LIVE",
            observed_at=observed_at,
        )

    def _load_snapshot(
        self,
        from_stop_id: str,
        to_stop_id: str,
        requested_date: str,
        mode_preference: str | None,
        snapshot_path: Path | str | None,
    ) -> tuple[TransferObservation, ...]:
        root = Path(__file__).resolve().parents[2]
        if snapshot_path:
            p = Path(snapshot_path)
        else:
            p = root / "docs" / "research" / "airport_parking_http_20261025" / "tfl_heathrow_mode_fares_20261025.json"

        if not p.is_file():
            raise TransferError("SNAPSHOT_NOT_FOUND", details=f"Snapshot file missing: {p}")

        raw = p.read_bytes()
        data = strict_json_loads(raw)

        observed_at = data.get("observed_at_utc", "2026-10-25T00:51:28.955908+00:00")

        # The mode_fares fixture has cases: "all_modes" (Heathrow Express) and "elizabeth_only"
        cases = data.get("cases", [])
        selected_journeys = []

        if mode_preference == "elizabeth-line":
            for c in cases:
                if c.get("name") == "elizabeth_only":
                    selected_journeys.extend(c.get("journeys", []))
        elif mode_preference == "national-rail":
            for c in cases:
                if c.get("name") == "all_modes":
                    selected_journeys.extend(c.get("journeys", []))
        else:
            # Combine both or use available
            for c in cases:
                selected_journeys.extend(c.get("journeys", []))

        if not selected_journeys:
            # Try alternate dated snapshot if available
            alt_path = (
                root
                / "docs"
                / "research"
                / "airport_parking_http_20261025"
                / "tfl_heathrow_to_paddington_dated_20261025.json"
            )
            if alt_path.is_file():
                alt_data = strict_json_loads(alt_path.read_bytes())
                selected_journeys = alt_data.get("sample_journeys", [])

        if not selected_journeys:
            raise TransferError("EMPTY_SCHEDULE", details="No journeys found in TfL snapshot")

        # Validate requested_date matches snapshot journeys
        matching_journeys = []
        available_dates = set()
        for j in selected_journeys:
            dep_raw = j.get("departure") or j.get("start")
            dep_local, _ = _format_local_timestamp(dep_raw, LONDON_TZ)
            if dep_local:
                s_date = dep_local.split("T")[0]
                available_dates.add(s_date)
                if s_date == requested_date:
                    matching_journeys.append(j)

        if not matching_journeys:
            dates_str = ", ".join(sorted(available_dates)) if available_dates else "unknown"
            raise SnapshotDateMismatchError(
                f"Requested travel date {requested_date} does not match snapshot service date ({dates_str})"
            )

        observations: list[TransferObservation] = []
        for j in matching_journeys[:6]:
            obs = self._parse_single_journey_snapshot(
                j,
                from_stop_id=from_stop_id,
                to_stop_id=to_stop_id,
                source_mode="OFFLINE_SNAPSHOT",
                observed_at=observed_at,
            )
            observations.append(obs)

        return tuple(observations)

    def _extract_disambiguation_choices(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        choices: list[dict[str, Any]] = []
        for key in ("fromLocationDisambiguation", "toLocationDisambiguation", "viaLocationDisambiguation"):
            dis = data.get(key)
            if isinstance(dis, dict):
                for ch in dis.get("choices", []):
                    choices.append(
                        {
                            "name": sanitize_text(ch.get("place_commonName")),
                            "id": ch.get("place_id"),
                        }
                    )
        return choices

    def _parse_journey_payload(
        self,
        payload: dict[str, Any],
        from_stop_id: str,
        to_stop_id: str,
        requested_date: str,
        source_mode: str,
        observed_at: str,
    ) -> tuple[TransferObservation, ...]:
        journeys = payload.get("journeys")
        if not journeys or not isinstance(journeys, list):
            raise TransferError("EMPTY_SCHEDULE", details="TfL response contained no journeys")

        results: list[TransferObservation] = []
        for j in journeys[:6]:
            dep_raw = j.get("startDateTime")
            arr_raw = j.get("arrivalDateTime")
            duration = j.get("duration")

            dep_local, utc_offset = _format_local_timestamp(dep_raw, LONDON_TZ)
            arr_local, _ = _format_local_timestamp(arr_raw, LONDON_TZ)

            # Date validation: fail closed if journey date does not match requested date
            if dep_local and requested_date:
                service_date = dep_local.split("T")[0]
                if service_date != requested_date:
                    continue

            legs = j.get("legs", [])
            # Corridor validation: if legs have departurePoint/arrivalPoint, verify they match
            if legs:
                first_leg = legs[0]
                if isinstance(first_leg, dict):
                    dep_pt = first_leg.get("departurePoint", {})
                    dep_naptan = dep_pt.get("naptanId") or dep_pt.get("id") if isinstance(dep_pt, dict) else None
                    if dep_naptan and str(dep_naptan) != str(from_stop_id):
                        continue

                last_leg = legs[-1]
                if isinstance(last_leg, dict):
                    arr_pt = last_leg.get("arrivalPoint", {})
                    arr_naptan = arr_pt.get("naptanId") or arr_pt.get("id") if isinstance(arr_pt, dict) else None
                    if arr_naptan and str(arr_naptan) != str(to_stop_id):
                        continue

            # Determine mode, operator, route — never fabricate Heathrow Express for arbitrary trains
            mode = "national-rail"
            operator = "UNKNOWN_OPERATOR"
            route_name = "UNKNOWN_OPERATOR"

            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                leg_mode = leg.get("mode", {})
                mode_id = leg_mode.get("id") if isinstance(leg_mode, dict) else str(leg_mode or "")
                mode_name = leg_mode.get("name") if isinstance(leg_mode, dict) else ""
                instr = leg.get("instruction", {})
                summary = instr.get("summary", "") if isinstance(instr, dict) else str(instr or "")
                detailed = instr.get("detailed", "") if isinstance(instr, dict) else ""
                route_options = leg.get("routeOptions", [])
                ro_names = [ro.get("name", "") for ro in route_options if isinstance(ro, dict)]
                leg_route = leg.get("route", "") or leg.get("route_name", "")

                leg_text = f"{mode_id} {mode_name} {summary} {detailed} {' '.join(ro_names)} {leg_route}".lower()

                if "elizabeth line" in leg_text or mode_id == "elizabeth-line":
                    mode = "elizabeth-line"
                    operator = "TfL Elizabeth line"
                    route_name = "Elizabeth line"
                    break
                elif "heathrow express" in leg_text:
                    mode = "national-rail"
                    operator = "Heathrow Express"
                    route_name = "Heathrow Express"
                    break

            # Parse fare
            fare_obj = j.get("fare")
            fare_total_cost = fare_obj.get("totalCost") if isinstance(fare_obj, dict) else None

            warnings: list[dict[str, str]] = []
            if source_mode == "OFFLINE_SNAPSHOT":
                warnings.append(
                    {
                        "code": "HISTORICAL_SNAPSHOT_NOT_LIVE",
                        "message": "Snapshot data from research fixtures, not a live booking quote",
                    }
                )

            warnings.append(
                {
                    "code": "PLANNER_FARE_NOT_CHECKOUT",
                    "message": "TfL Journey Planner fare is an estimate; checkout and seat not verified",
                }
            )
            warnings.append(
                {
                    "code": "TAX_AND_CONDITIONS_UNVERIFIED",
                    "message": "Peak/off-peak conditions, contactless caps, and taxes require checkout verification",
                }
            )
            warnings.append(
                {
                    "code": "LAST_MILE_NOT_INCLUDED",
                    "message": "Airport transfer terminates at London Paddington; onward travel to hotel is not included",
                }
            )

            if operator == "UNKNOWN_OPERATOR":
                warnings.append(
                    {
                        "code": "UNKNOWN_OPERATOR",
                        "message": "Train operator could not be verified as Heathrow Express or Elizabeth line",
                    }
                )

            if fare_total_cost is not None:
                fare_amount = pence_to_gbp(fare_total_cost)
                currency = "GBP"
                fare_basis = "ADULT_SINGLE_PLANNER"
                price_status = "PLANNER_FARE_NOT_CHECKOUT"
                fare_source = "TFL_JOURNEY_PLANNER"
            else:
                fare_amount = None
                currency = "GBP"
                fare_basis = None
                price_status = "UNKNOWN"
                fare_source = "UNKNOWN"
                warnings.append(
                    {
                        "code": "PRICE_UNKNOWN",
                        "message": "Fare totalCost missing from TfL journey result",
                    }
                )

            obs = TransferObservation(
                origin_stop_id=from_stop_id,
                origin_name=HEATHROW_T5_NAME,
                destination_stop_id=to_stop_id,
                destination_name=PADDINGTON_NAME,
                departure_local=dep_local,
                arrival_local=arr_local,
                duration_minutes=int(duration) if duration is not None else None,
                timezone_name="Europe/London",
                utc_offset=utc_offset,
                mode=mode,
                direction="airport_to_city",
                operator=operator,
                route_name=route_name,
                source_provider="TFL_JOURNEY_PLANNER",
                observed_at=observed_at,
                fare_amount=fare_amount,
                currency=currency,
                fare_basis=fare_basis,
                price_status=price_status,
                fare_source=fare_source,
                source_mode=source_mode,
                license_attribution=TFL_ATTRIBUTION,
                checkout_verified=False,
                warnings=tuple(warnings),
            )
            results.append(obs)

        if not results:
            raise TransferError("EMPTY_SCHEDULE", details=f"No valid TfL journeys found matching corridor and date {requested_date}")

        return tuple(results)

    def _parse_single_journey_snapshot(
        self,
        j: dict[str, Any],
        from_stop_id: str,
        to_stop_id: str,
        source_mode: str,
        observed_at: str,
    ) -> TransferObservation:
        # Fixture keys can be departure/arrival or start/arrival
        dep_raw = j.get("departure") or j.get("start")
        arr_raw = j.get("arrival")
        duration = j.get("duration_min") or j.get("duration_minutes")

        dep_local, utc_offset = _format_local_timestamp(dep_raw, LONDON_TZ)
        arr_local, _ = _format_local_timestamp(arr_raw, LONDON_TZ)

        legs = j.get("legs", [])
        mode = "national-rail"
        operator = "UNKNOWN_OPERATOR"
        route_name = "UNKNOWN_OPERATOR"

        for leg in legs:
            m = leg.get("mode", "")
            r = leg.get("route", "") or leg.get("route_name", "")
            if m == "elizabeth-line" or "Elizabeth" in r:
                mode = "elizabeth-line"
                operator = "TfL Elizabeth line"
                route_name = "Elizabeth line"
                break
            elif "Heathrow Express" in r:
                mode = "national-rail"
                operator = "Heathrow Express"
                route_name = "Heathrow Express"
                break

        # Fare in snapshot can be fare_totalCost or in fare dict
        fare_total_cost = j.get("fare_totalCost")
        if fare_total_cost is None and isinstance(j.get("fare"), dict):
            fare_total_cost = j["fare"].get("totalCost")

        warnings: list[dict[str, str]] = [
            {
                "code": "HISTORICAL_SNAPSHOT_NOT_LIVE",
                "message": "Snapshot data from research fixtures, not a live booking quote",
            },
            {
                "code": "PLANNER_FARE_NOT_CHECKOUT",
                "message": "TfL Journey Planner fare is an estimate; checkout and seat not verified",
            },
            {
                "code": "TAX_AND_CONDITIONS_UNVERIFIED",
                "message": "Peak/off-peak conditions, contactless caps, and taxes require checkout verification",
            },
            {
                "code": "LAST_MILE_NOT_INCLUDED",
                "message": "Airport transfer terminates at London Paddington; onward travel to hotel is not included",
            },
        ]
        if operator == "UNKNOWN_OPERATOR":
            warnings.append(
                {
                    "code": "UNKNOWN_OPERATOR",
                    "message": "National rail train operator could not be verified as Heathrow Express",
                }
            )

        if fare_total_cost is not None:
            fare_amount = pence_to_gbp(fare_total_cost)
            currency = "GBP"
            fare_basis = "ADULT_SINGLE_PLANNER"
            price_status = "PLANNER_FARE_NOT_CHECKOUT"
            fare_source = "TFL_JOURNEY_PLANNER"
        else:
            fare_amount = None
            currency = "GBP"
            fare_basis = None
            price_status = "UNKNOWN"
            fare_source = "UNKNOWN"
            warnings.append(
                {
                    "code": "PRICE_UNKNOWN",
                    "message": "Fare totalCost missing from TfL journey result",
                }
            )

        return TransferObservation(
            origin_stop_id=from_stop_id,
            origin_name=HEATHROW_T5_NAME,
            destination_stop_id=to_stop_id,
            destination_name=PADDINGTON_NAME,
            departure_local=dep_local,
            arrival_local=arr_local,
            duration_minutes=int(duration) if duration is not None else None,
            timezone_name="Europe/London",
            utc_offset=utc_offset,
            mode=mode,
            direction="airport_to_city",
            operator=operator,
            route_name=route_name,
            source_provider="TFL_JOURNEY_PLANNER",
            observed_at=observed_at,
            fare_amount=fare_amount,
            currency=currency,
            fare_basis=fare_basis,
            price_status=price_status,
            fare_source=fare_source,
            source_mode=source_mode,
            license_attribution=TFL_ATTRIBUTION,
            checkout_verified=False,
            warnings=tuple(warnings),
        )
