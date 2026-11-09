"""Independent overnight-arrival transfer and hotel check-in feasibility checker.

Specification:
- Scenario A05: Overnight arrival boundary check (docs/hackathon/2026-10-24/06_ACCEPTANCE.md)
- TripPlan v0 contract alignment (docs/contracts/TRIP_PLAN_V0.md)
- Transfer source models alignment (experimental/transfer_sources/models.py)

Principles:
1. NEVER invent timestamps, hotel check-in policies, or transfer schedules.
2. Inputs must come from explicitly provided, verified timestamps and policy windows.
3. Unknown or unverified inputs return status NEEDS_INPUT (never assumed SAFE).
4. Detected conflicts return EXPLAINED_CONFLICT with actionable options
   (contact_hotel, add_night, shift_transfer).
5. Costs in options must be explicitly "UNKNOWN" rather than fabricated.
6. Timestamp-aware: strictly requires timezone offsets (no naive datetimes).
7. Completely isolated, offline-only, zero network/supplier dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import re
from typing import Any

# ISO-8601 offset timestamp pattern (matching TripPlan v0)
OFFSET_TIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:Z|[+-](?:0\d|1[0-4]):[0-5]\d)$"
)
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_HHMM_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class FeasibilityStatus:
    FEASIBLE = "FEASIBLE"
    NEEDS_INPUT = "NEEDS_INPUT"
    EXPLAINED_CONFLICT = "EXPLAINED_CONFLICT"


class ActionType:
    CONTACT_HOTEL = "contact_hotel"
    ADD_NIGHT = "add_night"
    SHIFT_TRANSFER = "shift_transfer"


class ConflictCode:
    CHECKIN_BEFORE_ARRIVAL = "CHECKIN_BEFORE_ARRIVAL"
    ARRIVAL_BEFORE_CHECKIN_WINDOW = "ARRIVAL_BEFORE_CHECKIN_WINDOW"
    CHECKIN_WINDOW_EXPIRED = "CHECKIN_WINDOW_EXPIRED"
    TRANSFER_BEFORE_ARRIVAL = "TRANSFER_BEFORE_ARRIVAL"
    INSUFFICIENT_TRANSFER_BUFFER = "INSUFFICIENT_TRANSFER_BUFFER"
    HOTEL_CLOSED_AT_TRANSFER_ARRIVAL = "HOTEL_CLOSED_AT_TRANSFER_ARRIVAL"
    DEPARTURE_AFTER_ARRIVAL = "DEPARTURE_AFTER_ARRIVAL"
    UNCOVERED_NIGHTS_BEFORE_CHECKIN = "UNCOVERED_NIGHTS_BEFORE_CHECKIN"
    CHECKOUT_BEFORE_OR_AT_ARRIVAL = "CHECKOUT_BEFORE_OR_AT_ARRIVAL"
    INVALID_HOTEL_DATES = "INVALID_HOTEL_DATES"


class MissingInputCode:
    MISSING_FLIGHT_ARRIVAL = "MISSING_FLIGHT_ARRIVAL"
    MISSING_TIMEZONE_OFFSET = "MISSING_TIMEZONE_OFFSET"
    MISSING_HOTEL_CHECKIN_DATE = "MISSING_HOTEL_CHECKIN_DATE"
    MISSING_HOTEL_CHECKOUT_DATE = "MISSING_HOTEL_CHECKOUT_DATE"
    MISSING_HOTEL_POLICY = "MISSING_HOTEL_POLICY"
    MISSING_HOTEL_CHECKIN_START = "MISSING_HOTEL_CHECKIN_START"
    MISSING_TRANSFER_PICKUP = "MISSING_TRANSFER_PICKUP"
    UNVERIFIED_INPUT = "UNVERIFIED_INPUT"


@dataclass(frozen=True)
class ActionableOption:
    """Actionable resolution option with explicit UNKNOWN cost guarantee."""

    action: str  # "contact_hotel" | "add_night" | "shift_transfer"
    title: str
    description: str
    cost: str = "UNKNOWN"
    suggested_value: str | None = None

    def __post_init__(self) -> None:
        if self.action not in (
            ActionType.CONTACT_HOTEL,
            ActionType.ADD_NIGHT,
            ActionType.SHIFT_TRANSFER,
        ):
            raise ValueError(f"Invalid action type: {self.action}")
        if not isinstance(self.cost, str) or not self.cost.strip():
            raise ValueError("cost must be a non-empty string, default 'UNKNOWN'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "title": self.title,
            "description": self.description,
            "cost": self.cost,
            "suggested_value": self.suggested_value,
        }


@dataclass(frozen=True)
class ConflictDetail:
    """Detailed conflict information and actionable remedy options."""

    code: str
    component: str  # "hotel" | "transfer" | "transport"
    message: str
    actionable_options: tuple[ActionableOption, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "component": self.component,
            "message": self.message,
            "actionable_options": [opt.to_dict() for opt in self.actionable_options],
        }


@dataclass(frozen=True)
class FeasibilityVerdict:
    """Final assessment of overnight arrival and transfer/checkin feasibility."""

    status: str  # "FEASIBLE" | "NEEDS_INPUT" | "EXPLAINED_CONFLICT"
    is_feasible: bool
    summary: str
    conflicts: tuple[ConflictDetail, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "is_feasible": self.is_feasible,
            "summary": self.summary,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "missing_inputs": list(self.missing_inputs),
            "details": self.details,
        }


@dataclass(frozen=True)
class FlightArrivalInfo:
    """Verified flight timing information."""

    arrival_time: str | datetime | None
    departure_time: str | datetime | None = None
    origin_airport: str | None = None
    destination_airport: str | None = None
    is_verified: bool = False


@dataclass(frozen=True)
class HotelCheckinPolicy:
    """Verified hotel booking and check-in window policies."""

    checkin_date: str | date | None
    checkout_date: str | date | None
    hotel_name: str | None = None
    checkin_window_start: str | None = None  # e.g. "14:00" or "15:00"
    checkin_window_end: str | None = None    # e.g. "23:00" or "23:59"
    late_checkin_cutoff: str | None = None   # e.g. "04:00"
    has_24h_front_desk: bool | None = None
    allows_late_checkin: bool | None = None
    late_checkin_fee: str | None = None
    late_checkin_fee_currency: str | None = None
    is_policy_verified: bool = False


@dataclass(frozen=True)
class TransferScheduleInfo:
    """Verified transfer schedule information."""

    pickup_time: str | datetime | None = None
    dropoff_time: str | datetime | None = None
    duration_minutes: int | None = None
    mode: str | None = None
    provider: str | None = None
    fare_amount: str | None = None
    currency: str | None = None
    fare_scope: str = "one_way_arrival"
    is_schedule_verified: bool = False


def parse_offset_datetime(value: str | datetime | None, field_name: str = "timestamp") -> datetime | None:
    """Parse ISO-8601 string or datetime, strictly requiring timezone offset."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"{field_name} must have a timezone offset (naive datetimes not permitted)")
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be an ISO-8601 string or datetime")
    raw = value.strip()
    if not OFFSET_TIME_PATTERN.fullmatch(raw):
        raise ValueError(
            f"{field_name} must be formatted as ISO-8601 with timezone offset (e.g. '2026-11-13T00:30:00+04:00' or 'Z')"
        )
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} lacks timezone offset")
    return parsed


def parse_iso_date(value: str | date | None, field_name: str = "date") -> date | None:
    """Parse YYYY-MM-DD string or date."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a YYYY-MM-DD string or date")
    raw = value.strip()
    if not DATE_PATTERN.fullmatch(raw):
        raise ValueError(f"{field_name} must be formatted as YYYY-MM-DD")
    return date.fromisoformat(raw)


def parse_hhmm_time(value: str | None, field_name: str = "time") -> time | None:
    """Parse HH:MM string."""
    if value is None:
        return None
    if isinstance(value, time):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a HH:MM string")
    raw = value.strip()
    if not TIME_HHMM_PATTERN.fullmatch(raw):
        raise ValueError(f"{field_name} must be formatted as HH:MM (24-hour)")
    parts = raw.split(":")
    return time(hour=int(parts[0]), minute=int(parts[1]))


def check_overnight_feasibility(
    flight: FlightArrivalInfo | dict[str, Any],
    hotel: HotelCheckinPolicy | dict[str, Any],
    transfer: TransferScheduleInfo | dict[str, Any] | None = None,
    min_airport_buffer_minutes: int = 45,
) -> FeasibilityVerdict:
    """Check feasibility of overnight arrival, transfer schedule, and hotel check-in.

    Enforces strict zero-fabrication:
    - Never invents missing timestamps, policies, or prices.
    - Missing data yields NEEDS_INPUT.
    - Incompatibilities yield EXPLAINED_CONFLICT with actionable options.
    - All options feature cost='UNKNOWN' unless verified fee was explicitly provided.
    """
    conflicts: list[ConflictDetail] = []
    missing_inputs: list[str] = []

    # 1. Normalize and verify Flight input
    if isinstance(flight, dict):
        fl_verified = flight.get("is_verified", False)
        fl_arrival_raw = flight.get("arrival_time") or flight.get("arrival_at")
        fl_departure_raw = flight.get("departure_time") or flight.get("departure_at")
    elif isinstance(flight, FlightArrivalInfo):
        fl_verified = flight.is_verified
        fl_arrival_raw = flight.arrival_time
        fl_departure_raw = flight.departure_time
    else:
        raise TypeError("flight must be FlightArrivalInfo or dict")

    if fl_verified is not True:
        return FeasibilityVerdict(
            status=FeasibilityStatus.NEEDS_INPUT,
            is_feasible=False,
            summary="Flight timing is marked unverified. Refusing to assess unverified flight timestamps.",
            missing_inputs=(MissingInputCode.UNVERIFIED_INPUT,),
        )

    if fl_arrival_raw is None:
        missing_inputs.append("flight.arrival_time")
        arrival_dt = None
    else:
        try:
            arrival_dt = parse_offset_datetime(fl_arrival_raw, "flight.arrival_time")
        except ValueError as e:
            return FeasibilityVerdict(
                status=FeasibilityStatus.NEEDS_INPUT,
                is_feasible=False,
                summary=f"Flight arrival timestamp format invalid or naive: {e}",
                missing_inputs=(MissingInputCode.MISSING_TIMEZONE_OFFSET,),
            )

    departure_dt = None
    if fl_departure_raw is not None:
        try:
            departure_dt = parse_offset_datetime(fl_departure_raw, "flight.departure_time")
        except ValueError:
            missing_inputs.append("flight.departure_time_offset")

    # If departure and arrival are both available, verify flight duration in UTC
    if departure_dt is not None and arrival_dt is not None:
        elapsed_seconds = (arrival_dt - departure_dt).total_seconds()
        if elapsed_seconds <= 0:
            conflicts.append(
                ConflictDetail(
                    code=ConflictCode.DEPARTURE_AFTER_ARRIVAL,
                    component="transport",
                    message=(
                        f"Flight departure instant ({departure_dt.isoformat()}) is after or equal to "
                        f"arrival instant ({arrival_dt.isoformat()}). Verify timezone offsets and travel dates."
                    ),
                    actionable_options=(),
                )
            )

    # 2. Normalize and verify Hotel input
    if isinstance(hotel, dict):
        ht_verified = hotel.get("is_policy_verified", False)
        ht_checkin_raw = hotel.get("checkin_date") or hotel.get("checkin")
        ht_checkout_raw = hotel.get("checkout_date") or hotel.get("checkout")
        ht_name = hotel.get("hotel_name") or hotel.get("name")
        ht_win_start = hotel.get("checkin_window_start")
        ht_win_end = hotel.get("checkin_window_end")
        ht_late_cutoff = hotel.get("late_checkin_cutoff")
        ht_24h = hotel.get("has_24h_front_desk")
        ht_late_allow = hotel.get("allows_late_checkin")
        ht_late_fee = hotel.get("late_checkin_fee")
        ht_late_currency = hotel.get("late_checkin_fee_currency")
    elif isinstance(hotel, HotelCheckinPolicy):
        ht_verified = hotel.is_policy_verified
        ht_checkin_raw = hotel.checkin_date
        ht_checkout_raw = hotel.checkout_date
        ht_name = hotel.hotel_name
        ht_win_start = hotel.checkin_window_start
        ht_win_end = hotel.checkin_window_end
        ht_late_cutoff = hotel.late_checkin_cutoff
        ht_24h = hotel.has_24h_front_desk
        ht_late_allow = hotel.allows_late_checkin
        ht_late_fee = hotel.late_checkin_fee
        ht_late_currency = hotel.late_checkin_fee_currency
    else:
        raise TypeError("hotel must be HotelCheckinPolicy or dict")

    if ht_verified is not True:
        return FeasibilityVerdict(
            status=FeasibilityStatus.NEEDS_INPUT,
            is_feasible=False,
            summary="Hotel policy is marked unverified. Refusing to assess unverified hotel parameters.",
            missing_inputs=(MissingInputCode.UNVERIFIED_INPUT,),
        )

    try:
        checkin_d = parse_iso_date(ht_checkin_raw, "hotel.checkin_date")
    except ValueError:
        checkin_d = None
        missing_inputs.append("hotel.checkin_date")

    try:
        checkout_d = parse_iso_date(ht_checkout_raw, "hotel.checkout_date")
    except ValueError:
        checkout_d = None
        missing_inputs.append("hotel.checkout_date")

    if checkin_d is None:
        missing_inputs.append("hotel.checkin_date")
    if checkout_d is None:
        missing_inputs.append("hotel.checkout_date")

    if checkin_d is not None and checkout_d is not None:
        if checkin_d >= checkout_d:
            conflicts.append(
                ConflictDetail(
                    code=ConflictCode.INVALID_HOTEL_DATES,
                    component="hotel",
                    message=f"Hotel checkin date ({checkin_d}) must be strictly before checkout date ({checkout_d}).",
                    actionable_options=(),
                )
            )

    # 3. Normalize Transfer input
    tr_obj = None
    if transfer is not None:
        if isinstance(transfer, dict):
            tr_verified = transfer.get("is_schedule_verified", False)
            tr_pickup_raw = transfer.get("pickup_time") or transfer.get("pickup_at")
            tr_duration = transfer.get("duration_minutes")
            tr_provider = transfer.get("provider")
        elif isinstance(transfer, TransferScheduleInfo):
            tr_verified = transfer.is_schedule_verified
            tr_pickup_raw = transfer.pickup_time
            tr_duration = transfer.duration_minutes
            tr_provider = transfer.provider
        else:
            raise TypeError("transfer must be TransferScheduleInfo, dict, or None")

        if tr_verified is not True:
            return FeasibilityVerdict(
                status=FeasibilityStatus.NEEDS_INPUT,
                is_feasible=False,
                summary="Transfer schedule is marked unverified. Refusing to assess unverified transfer schedule.",
                missing_inputs=(MissingInputCode.UNVERIFIED_INPUT,),
            )

        tr_pickup_dt = None
        if tr_pickup_raw is None:
            missing_inputs.append("transfer.pickup_time")
        else:
            try:
                tr_pickup_dt = parse_offset_datetime(tr_pickup_raw, "transfer.pickup_time")
            except ValueError as e:
                return FeasibilityVerdict(
                    status=FeasibilityStatus.NEEDS_INPUT,
                    is_feasible=False,
                    summary=f"Transfer pickup timestamp format invalid or naive: {e}",
                    missing_inputs=(MissingInputCode.MISSING_TIMEZONE_OFFSET,),
                )

        tr_obj = {
            "pickup_dt": tr_pickup_dt,
            "duration": tr_duration,
            "provider": tr_provider,
        }

    # If critical arrival timestamp is missing, return early
    if arrival_dt is None:
        return FeasibilityVerdict(
            status=FeasibilityStatus.NEEDS_INPUT,
            is_feasible=False,
            summary="Flight arrival timestamp is required to determine feasibility.",
            missing_inputs=tuple(missing_inputs),
        )

    # Local date and time at destination
    local_arr_date = arrival_dt.date()
    local_arr_time = arrival_dt.time()
    is_overnight = (local_arr_time.hour < 7 or local_arr_time.hour >= 22)

    # 4. Detailed Hotel Feasibility Checking
    if checkin_d is not None and checkout_d is not None:
        if checkout_d <= local_arr_date:
            conflicts.append(
                ConflictDetail(
                    code=ConflictCode.CHECKOUT_BEFORE_OR_AT_ARRIVAL,
                    component="hotel",
                    message=(
                        f"Hotel checkout date ({checkout_d.isoformat()}) is on or before local arrival date "
                        f"({local_arr_date.isoformat()}). The traveler has no active booking upon arrival."
                    ),
                    actionable_options=(
                        ActionableOption(
                            action=ActionType.ADD_NIGHT,
                            title="Book room covering arrival dates",
                            description=f"Book accommodation starting on or before {local_arr_date.isoformat()} through your stay.",
                            cost="UNKNOWN",
                            suggested_value=local_arr_date.isoformat(),
                        ),
                    ),
                )
            )

        # Case A: Checkin is on the previous calendar date (Checkin Day D, Arrival Day D+1)
        # Canonical A05 scenario: flight arrives after midnight (e.g. Nov 13 00:30), hotel booked from Nov 12
        elif (local_arr_date - checkin_d).days == 1 and is_overnight:
            # Traveler lands overnight for a room booked from yesterday.
            # We must verify hotel late check-in / 24h reception policy.
            # NEVER assume 24h reception or open checkin without verified policy!
            policy_known = (
                ht_24h is not None
                or ht_late_allow is not None
                or ht_win_end is not None
                or ht_late_cutoff is not None
            )

            if not policy_known:
                missing_inputs.append("hotel.late_checkin_policy")
            else:
                if ht_24h is True:
                    # 24-hour reception desk is verified.
                    # Traveler can check in, but requires advance hotel notification to prevent no-show release.
                    # This is technically feasible, but check transfer dropoff timing if available.
                    pass
                elif ht_24h is False and ht_late_allow is False:
                    # Front desk closed, late check-in not allowed.
                    fee_cost = (
                        f"{ht_late_fee} {ht_late_currency or ''}".strip()
                        if ht_late_fee
                        else "UNKNOWN"
                    )
                    conflicts.append(
                        ConflictDetail(
                            code=ConflictCode.CHECKIN_WINDOW_EXPIRED,
                            component="hotel",
                            message=(
                                f"Hotel does not have a 24-hour front desk and does not permit late check-in. "
                                f"Flight arrives at {local_arr_time.strftime('%H:%M')} on {local_arr_date.isoformat()}, "
                                f"after check-in closed on {checkin_d.isoformat()}."
                            ),
                            actionable_options=(
                                ActionableOption(
                                    action=ActionType.CONTACT_HOTEL,
                                    title="Contact hotel for after-hours check-in arrangement",
                                    description=(
                                        f"Contact {ht_name or 'the hotel'} to negotiate after-hours key access, lockbox code, "
                                        f"or on-call night reception for post-midnight arrival on {checkin_d.isoformat()} reservation."
                                    ),
                                    cost=fee_cost,
                                ),
                                ActionableOption(
                                    action=ActionType.ADD_NIGHT,
                                    title="Book alternative lodging with 24-hour reception",
                                    description="Book hotel offering confirmed 24-hour front desk for seamless overnight arrival.",
                                    cost="UNKNOWN",
                                ),
                            ),
                        )
                    )
                elif ht_late_cutoff is not None or ht_win_end is not None:
                    cutoff_str = ht_late_cutoff or ht_win_end
                    cutoff_t = parse_hhmm_time(cutoff_str, "hotel.late_cutoff")
                    if cutoff_t is not None:
                        # Construct cutoff datetime in destination timezone
                        # If cutoff is early morning (e.g. 02:00, 04:00), it applies to arrival date morning
                        if cutoff_t < time(hour=12):
                            cutoff_dt = datetime.combine(local_arr_date, cutoff_t, tzinfo=arrival_dt.tzinfo)
                        else:
                            cutoff_dt = datetime.combine(checkin_d, cutoff_t, tzinfo=arrival_dt.tzinfo)

                        # Determine expected hotel arrival (transfer dropoff or flight arrival + min buffer)
                        est_hotel_arr = arrival_dt + timedelta(minutes=min_airport_buffer_minutes)
                        if tr_obj and tr_obj.get("pickup_dt") and tr_obj.get("duration"):
                            est_hotel_arr = tr_obj["pickup_dt"] + timedelta(minutes=tr_obj["duration"])

                        if est_hotel_arr > cutoff_dt:
                            fee_cost = (
                                f"{ht_late_fee} {ht_late_currency or ''}".strip()
                                if ht_late_fee
                                else "UNKNOWN"
                            )
                            conflicts.append(
                                ConflictDetail(
                                    code=ConflictCode.CHECKIN_WINDOW_EXPIRED,
                                    component="hotel",
                                    message=(
                                        f"Estimated hotel arrival ({est_hotel_arr.strftime('%H:%M %Z')}) exceeds hotel "
                                        f"late check-in cutoff ({cutoff_dt.strftime('%H:%M %Z')} on {cutoff_dt.date().isoformat()})."
                                    ),
                                    actionable_options=(
                                        ActionableOption(
                                            action=ActionType.CONTACT_HOTEL,
                                            title="Request late check-in exception",
                                            description=f"Notify {ht_name or 'hotel'} of flight arrival at {local_arr_time.strftime('%H:%M')} and request late arrival lockbox/key arrangement.",
                                            cost=fee_cost,
                                        ),
                                    ),
                                )
                            )

        # Case B: Checkin is on the same calendar date as arrival (Checkin Day D, Arrival Day D)
        elif checkin_d == local_arr_date:
            if ht_win_start is not None:
                win_start_t = parse_hhmm_time(ht_win_start, "hotel.checkin_window_start")
                if win_start_t is not None:
                    win_start_dt = datetime.combine(checkin_d, win_start_t, tzinfo=arrival_dt.tzinfo)
                    if arrival_dt < win_start_dt:
                        prev_night = checkin_d - timedelta(days=1)
                        conflicts.append(
                            ConflictDetail(
                                code=ConflictCode.ARRIVAL_BEFORE_CHECKIN_WINDOW,
                                component="hotel",
                                message=(
                                    f"Flight arrives at {local_arr_time.strftime('%H:%M')} on {local_arr_date.isoformat()}, "
                                    f"but hotel check-in for {checkin_d.isoformat()} does not open until {ht_win_start}. "
                                    f"Guest arrives ~{int((win_start_dt - arrival_dt).total_seconds() / 3600)} hours before the room is ready."
                                ),
                                actionable_options=(
                                    ActionableOption(
                                        action=ActionType.ADD_NIGHT,
                                        title="Book previous night for immediate room availability",
                                        description=(
                                            f"Book an additional night starting {prev_night.isoformat()} so the hotel room is "
                                            f"reserved and ready immediately upon arrival on {local_arr_date.isoformat()}."
                                        ),
                                        cost="UNKNOWN",
                                        suggested_value=prev_night.isoformat(),
                                    ),
                                    ActionableOption(
                                        action=ActionType.CONTACT_HOTEL,
                                        title="Request early check-in or luggage storage",
                                        description=(
                                            f"Contact {ht_name or 'hotel'} to request early check-in (subject to availability) "
                                            f"or arrange luggage drop-off at reception until {ht_win_start}."
                                        ),
                                        cost="UNKNOWN",
                                    ),
                                ),
                            )
                        )
            elif is_overnight and local_arr_time.hour < 12:
                # Arriving early morning / overnight on checkin date without checkin window start
                missing_inputs.append("hotel.checkin_window_start")

        # Case C: Checkin is AFTER arrival date (uncovered nights)
        elif checkin_d > local_arr_date:
            conflicts.append(
                ConflictDetail(
                    code=ConflictCode.UNCOVERED_NIGHTS_BEFORE_CHECKIN,
                    component="hotel",
                    message=(
                        f"Flight arrives on {local_arr_date.isoformat()}, but hotel reservation starts on "
                        f"{checkin_d.isoformat()}. Night(s) between arrival and check-in are uncovered."
                    ),
                    actionable_options=(
                        ActionableOption(
                            action=ActionType.ADD_NIGHT,
                            title="Add accommodation for arrival night",
                            description=f"Book hotel night starting {local_arr_date.isoformat()} to cover accommodation before {checkin_d.isoformat()}.",
                            cost="UNKNOWN",
                            suggested_value=local_arr_date.isoformat(),
                        ),
                    ),
                )
            )

        # Case D: Checkin is 2 or more days BEFORE arrival date
        elif (local_arr_date - checkin_d).days > 1:
            conflicts.append(
                ConflictDetail(
                    code=ConflictCode.CHECKIN_BEFORE_ARRIVAL,
                    component="hotel",
                    message=(
                        f"Hotel checkin date ({checkin_d.isoformat()}) is {(local_arr_date - checkin_d).days} days "
                        f"before flight arrival date ({local_arr_date.isoformat()}). Unattended reservation risks cancellation."
                    ),
                    actionable_options=(
                        ActionableOption(
                            action=ActionType.CONTACT_HOTEL,
                            title="Notify hotel of delayed arrival",
                            description="Contact hotel to prevent automatic no-show cancellation of multi-day reservation.",
                            cost="UNKNOWN",
                        ),
                    ),
                )
            )

    # 5. Transfer schedule checks
    if tr_obj is not None:
        tr_pickup = tr_obj.get("pickup_dt")
        if tr_pickup is not None:
            # Check 1: Transfer pickup before flight arrival
            if tr_pickup < arrival_dt:
                suggested_pickup = arrival_dt + timedelta(minutes=min_airport_buffer_minutes)
                conflicts.append(
                    ConflictDetail(
                        code=ConflictCode.TRANSFER_BEFORE_ARRIVAL,
                        component="transfer",
                        message=(
                            f"Transfer pickup ({tr_pickup.isoformat()}) is scheduled before flight arrival "
                            f"({arrival_dt.isoformat()}). Physical impossibility."
                        ),
                        actionable_options=(
                            ActionableOption(
                                action=ActionType.SHIFT_TRANSFER,
                                title="Reschedule transfer pickup after flight arrival",
                                description=(
                                    f"Shift transfer pickup to at least {suggested_pickup.isoformat()} "
                                    f"to allow {min_airport_buffer_minutes} minutes for disembarkation and baggage retrieval."
                                ),
                                cost="UNKNOWN",
                                suggested_value=suggested_pickup.isoformat(),
                            ),
                        ),
                    )
                )
            # Check 2: Transfer pickup buffer insufficient
            elif tr_pickup < arrival_dt + timedelta(minutes=min_airport_buffer_minutes):
                buffer_actual = int((tr_pickup - arrival_dt).total_seconds() / 60)
                suggested_pickup = arrival_dt + timedelta(minutes=min_airport_buffer_minutes)
                conflicts.append(
                    ConflictDetail(
                        code=ConflictCode.INSUFFICIENT_TRANSFER_BUFFER,
                        component="transfer",
                        message=(
                            f"Transfer pickup ({tr_pickup.strftime('%H:%M')}) is only {buffer_actual} minutes after flight arrival "
                            f"({arrival_dt.strftime('%H:%M')}). Minimum buffer of {min_airport_buffer_minutes} minutes is required."
                        ),
                        actionable_options=(
                            ActionableOption(
                                action=ActionType.SHIFT_TRANSFER,
                                title="Shift transfer pickup to safe buffer window",
                                description=f"Reschedule transfer pickup to at least {suggested_pickup.isoformat()}.",
                                cost="UNKNOWN",
                                suggested_value=suggested_pickup.isoformat(),
                            ),
                        ),
                    )
                )

            # Check 3: Transfer arrival vs hotel front desk closing
            if (
                tr_obj.get("duration") is not None
                and ht_24h is False
                and ht_win_end is not None
                and checkin_d is not None
            ):
                tr_dropoff = tr_pickup + timedelta(minutes=tr_obj["duration"])
                win_end_t = parse_hhmm_time(ht_win_end, "hotel.checkin_window_end")
                if win_end_t is not None:
                    # Construct window end instant
                    win_end_dt = datetime.combine(checkin_d, win_end_t, tzinfo=arrival_dt.tzinfo)
                    if tr_dropoff > win_end_dt:
                        conflicts.append(
                            ConflictDetail(
                                code=ConflictCode.HOTEL_CLOSED_AT_TRANSFER_ARRIVAL,
                                component="transfer",
                                message=(
                                    f"Transfer dropoff at hotel ({tr_dropoff.strftime('%H:%M %Z')}) arrives after "
                                    f"hotel front desk check-in closes at {ht_win_end}."
                                ),
                                actionable_options=(
                                    ActionableOption(
                                        action=ActionType.CONTACT_HOTEL,
                                        title="Arrange after-hours hotel key access",
                                        description="Contact hotel to request after-hours key locker or night porter service.",
                                        cost="UNKNOWN",
                                    ),
                                    ActionableOption(
                                        action=ActionType.SHIFT_TRANSFER,
                                        title="Shift travel schedule",
                                        description="Re-examine transport options to arrive during front desk operating hours.",
                                        cost="UNKNOWN",
                                    ),
                                ),
                            )
                        )

    # 6. Synthesize final verdict
    details_dict = {
        "local_arrival_date": local_arr_date.isoformat(),
        "local_arrival_time": local_arr_time.strftime("%H:%M:%S"),
        "is_overnight_arrival": is_overnight,
        "timezone_offset": arrival_dt.strftime("%z"),
    }
    if departure_dt is not None:
        details_dict["flight_duration_minutes"] = int((arrival_dt - departure_dt).total_seconds() / 60)
        details_dict["crosses_midnight"] = arrival_dt.date() > departure_dt.date()

    if conflicts:
        status = FeasibilityStatus.EXPLAINED_CONFLICT
        is_feasible = False
        summary = (
            f"Detected {len(conflicts)} feasibility conflict(s) across overnight flight arrival, "
            f"transfer schedule, and hotel check-in window."
        )
    elif missing_inputs:
        status = FeasibilityStatus.NEEDS_INPUT
        is_feasible = False
        summary = (
            f"Feasibility cannot be confirmed. Missing or unverified critical inputs: "
            f"{', '.join(missing_inputs)}. Never assuming safe check-in without verified policy."
        )
    else:
        status = FeasibilityStatus.FEASIBLE
        is_feasible = True
        summary = (
            "Overnight arrival, airport transfer, and hotel check-in window are verified and feasible."
        )

    return FeasibilityVerdict(
        status=status,
        is_feasible=is_feasible,
        summary=summary,
        conflicts=tuple(conflicts),
        missing_inputs=tuple(missing_inputs),
        details=details_dict,
    )


def check_trip_plan_feasibility(
    trip_plan: dict[str, Any],
    hotel_policy: dict[str, Any] | None = None,
    min_airport_buffer_minutes: int = 45,
) -> FeasibilityVerdict:
    """Evaluate feasibility of an existing TripPlan v0 dictionary.

    Conforms to docs/contracts/TRIP_PLAN_V0.md.
    """
    transport = trip_plan.get("transport") or {}
    hotel = trip_plan.get("hotel") or {}
    transfer = trip_plan.get("transfer")

    fl_info = FlightArrivalInfo(
        arrival_time=transport.get("arrival_at"),
        departure_time=transport.get("departure_at"),
        origin_airport=trip_plan.get("origin"),
        destination_airport=trip_plan.get("destination"),
        is_verified=transport.get("source_mode") in ("MOCK", "LIVE", "OFFLINE_SNAPSHOT"),
    )

    combined_hotel: dict[str, Any] = {
        "checkin_date": hotel.get("checkin"),
        "checkout_date": hotel.get("checkout"),
        "hotel_name": hotel.get("name"),
        "is_policy_verified": True,
    }
    if hotel_policy:
        combined_hotel.update(hotel_policy)

    tr_info = None
    if transfer:
        tr_info = TransferScheduleInfo(
            pickup_time=transfer.get("pickup_at"),
            duration_minutes=transfer.get("duration_minutes"),
            provider=transfer.get("provider"),
            fare_amount=transfer.get("amount"),
            currency=transfer.get("currency"),
            fare_scope=transfer.get("fare_scope", "one_way_arrival"),
            is_schedule_verified=transfer.get("source_mode") in ("MOCK", "LIVE", "OFFLINE_SNAPSHOT"),
        )

    return check_overnight_feasibility(
        flight=fl_info,
        hotel=combined_hotel,
        transfer=tr_info,
        min_airport_buffer_minutes=min_airport_buffer_minutes,
    )
