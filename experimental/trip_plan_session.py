"""Dynamic session-aware TripPlan assembler and provider integration (#44/#48/#118).

Preserves exact currency/cents, sources, current revision, guests, dates,
destination, origin, and budget scope. Fails closed if critical data is unknown.

LIVE_FIRST Policy:
- Never presents stale, sandbox, or MOCK prices as current live fares.
- Never invents flights, transfers, or hotel inventory.
- No payment or booking execution.
- If realistic multi-provider data is not available, exposes a structured
  needs_input / partial status with explicit missing fields and questions
  rather than fabricating a total.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

try:
    from .trip_plan import (
        TripPlanError,
        assemble_trip_plan,
        budget_status,
        parse_amount,
    )
except ImportError:
    from trip_plan import (
        TripPlanError,
        assemble_trip_plan,
        budget_status,
        parse_amount,
    )

# --- Regex & Constants ---

DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OFFSET_TIME_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:Z|[+-](?:0\d|1[0-4]):[0-5]\d)$"
)
CURRENCY_REGEX = re.compile(r"^[A-Z]{3}$")

REQUIRED_COMPONENTS = ("hotel", "transport", "transfer")
OPTIONAL_COMPONENTS = ("insurance",)
VALID_BUDGET_SCOPES = frozenset({"whole_trip", "hotel_only", "unspecified"})
MOCK_MODES = frozenset({"MOCK", "ARCHIVE", "SANDBOX", "SYNTHETIC", "TEST", "UNAVAILABLE"})

UNKNOWN_PRICE_MESSAGES = {
    "hotel": "Цена жилья неизвестна. В итог она не входит: это не 0 и не замена другим тарифом.",
    "transport": "Цена перелёта неизвестна. В итог она не входит: это не 0 и не замена другим тарифом.",
    "transfer": "Цена трансфера неизвестна. В итог она не входит: это не 0 и не замена другим тарифом.",
    "insurance": "Цена страховки неизвестна. В итог она не входит: это не 0 и не замена другим тарифом.",
}


# --- Error Classes ---


class TripPlanSessionError(TripPlanError):
    """Base error for session-aware TripPlan assembly and verification."""

    def __init__(self, code: str, message: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(code)
        self.code = code
        self.message = message or code
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        result = {"error": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


class TripPlanContextUnknownError(TripPlanSessionError):
    """Raised when critical session context (e.g. origin, budget scope) is unknown."""

    def __init__(self, code: str = "TRIP_PLAN_CONTEXT_UNKNOWN", message: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(code, message or "Critical trip plan context is unknown.", details)


class TripPlanContextMismatchError(TripPlanSessionError):
    """Raised when session context does not match expected plan conditions."""

    def __init__(self, code: str = "TRIP_PLAN_CONTEXT_MISMATCH", message: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__(code, message or "Session context does not match trip plan.", details)


class StaleRevisionError(TripPlanSessionError):
    """Raised when session revision has moved or does not match expected revision."""

    def __init__(self, message: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__("STALE_REVISION", message or "Trip revision is stale.", details)


class LiveFirstViolationError(TripPlanSessionError):
    """Raised when sandbox or mock data is falsely presented as live pricing."""

    def __init__(self, message: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        super().__init__("LIVE_FIRST_VIOLATION", message or "MOCK or ARCHIVE pricing cannot be presented as LIVE.", details)


# --- Basic Validators ---


def validate_city_name(value: Any, field_name: str = "city") -> str:
    """Validate non-empty city name containing letters (rejects '??????' or blanks)."""
    if not isinstance(value, str):
        raise TripPlanSessionError("PLAN_FIELDS", f"{field_name} must be a string.")
    cleaned = value.strip()
    if not cleaned:
        raise TripPlanSessionError("PLAN_FIELDS", f"{field_name} cannot be empty.")
    # Check for presence of at least one letter character
    has_letter = False
    for char in cleaned:
        if char.isalpha():
            has_letter = True
            break
    if not has_letter:
        raise TripPlanSessionError("PLAN_FIELDS", f"{field_name} must contain letters.")
    return cleaned


def validate_date_str(value: Any, field_name: str = "date") -> str:
    """Validate YYYY-MM-DD string."""
    if not isinstance(value, str) or DATE_REGEX.fullmatch(value) is None:
        raise TripPlanSessionError("DATE", f"{field_name} must be YYYY-MM-DD string.")
    try:
        date.fromisoformat(value)
    except ValueError as err:
        raise TripPlanSessionError("DATE", f"Invalid calendar date for {field_name}.") from err
    return value


def validate_offset_time_str(value: Any, field_name: str = "time") -> str:
    """Validate ISO-8601 timestamp with explicit timezone offset."""
    if not isinstance(value, str) or OFFSET_TIME_REGEX.fullmatch(value) is None:
        raise TripPlanSessionError("TIME_OFFSET", f"{field_name} must have timezone offset.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise TripPlanSessionError("TIME_OFFSET", f"{field_name} requires timezone info.")
    except ValueError as err:
        raise TripPlanSessionError("TIME_OFFSET", f"Invalid timestamp for {field_name}.") from err
    return value


def validate_currency_code(value: Any) -> str:
    """Validate 3-letter ISO 4217 currency code."""
    if not isinstance(value, str) or CURRENCY_REGEX.fullmatch(value) is None:
        raise TripPlanSessionError("CURRENCY", "Currency must be 3 uppercase letters.")
    return value


def format_cents_amount(value: Any) -> Optional[str]:
    """Convert an amount to exact decimal string 'X.YY', or None if unpriced."""
    if value is None:
        return None
    cents = parse_amount(value)
    return format(cents, "f")


# --- Session Context Data Structure ---


@dataclass(frozen=True)
class SessionContext:
    """Normalized, validated trip context derived from application session."""

    sid: str
    revision: int
    destination: str
    start_date: str
    end_date: str
    currency: str
    adults: int
    children: Tuple[int, ...]
    origin: Optional[str]
    budget_amount: Optional[str]
    budget_scope: str  # "whole_trip", "hotel_only", or "unspecified"
    budget_currency: str
    rooms: Tuple[Tuple[int, Tuple[int, ...]], ...]
    country: Optional[str] = None
    raw_budget: Optional[str] = None

    def travellers_dict(self) -> Dict[str, Any]:
        return {"adults": self.adults, "children": list(self.children)}

    def rooms_dict(self) -> List[Dict[str, Any]]:
        return [{"adults": adults, "children": list(children)} for adults, children in self.rooms]

    def budget_dict(self) -> Dict[str, Any]:
        return {
            "amount": self.budget_amount,
            "currency": self.budget_currency,
            "scope": self.budget_scope,
        }

    def is_origin_known(self) -> bool:
        return bool(self.origin and self.origin.strip() and any(c.isalpha() for c in self.origin))

    def is_budget_scope_known(self) -> bool:
        if self.budget_amount is None:
            return self.budget_scope == "unspecified"
        return self.budget_scope in ("whole_trip", "hotel_only")


# --- Session Context Extraction ---


def extract_session_context(
    session_source: Any,
    *,
    origin: Optional[str] = None,
    budget_scope: Optional[str] = None,
    session_id: Optional[str] = None,
) -> SessionContext:
    """Extract and validate normalized trip context from a Session, State, or dict.

    Accepts:
    - prototype.session.Session
    - prototype.session.State
    - dict matching storage schema (e.g. store.get_session(sid))
    - dict with request / state fields
    """
    sid = session_id
    revision = 0
    raw_request = None
    source_origin = origin
    source_budget_scope = budget_scope
    country = None

    # Inspect source object structure
    if hasattr(session_source, "state"):
        sid = sid or getattr(session_source, "sid", None)
        state = session_source.state
        revision = getattr(state, "revision", 0)
        req = getattr(state, "request", None)
        raw_request = req
    elif hasattr(session_source, "request") and hasattr(session_source, "revision"):
        revision = session_source.revision
        raw_request = session_source.request
    elif isinstance(session_source, dict):
        embedded_sid = session_source.get("sid", session_source.get("id"))
        if sid is not None and embedded_sid is not None and sid != embedded_sid:
            raise TripPlanSessionError("SESSION_ID_MISMATCH")
        sid = sid or embedded_sid
        state_dict = session_source.get("state", session_source)
        if not isinstance(state_dict, dict):
            raise TripPlanSessionError("PLAN_FIELDS")
        revision = state_dict.get("revision", session_source.get("revision", 0))
        raw_request = state_dict.get("request", session_source.get("request", session_source))

        # Check for origin / budget_scope in state or session top-level
        if source_origin is None:
            source_origin = (
                session_source.get("origin")
                or state_dict.get("origin")
                or (isinstance(raw_request, dict) and raw_request.get("origin"))
            )
        if source_budget_scope is None:
            source_budget_scope = (
                session_source.get("budget_scope")
                or state_dict.get("budget_scope")
                or (isinstance(raw_request, dict) and raw_request.get("budget_scope"))
            )
    else:
        raise TripPlanSessionError("PLAN_TYPE", "Unsupported session source type.")

    if not isinstance(sid, str) or not sid.strip() or sid != sid.strip():
        raise TripPlanSessionError("SESSION_ID_REQUIRED", "An actual session id is required.")
    if type(revision) is not int or revision < 0:
        raise TripPlanSessionError("REVISION", "Revision must be non-negative integer.")

    # Extract request fields
    if hasattr(raw_request, "city"):
        city_raw = raw_request.city
        country = getattr(raw_request, "country", None)
        checkin_raw = raw_request.checkin
        checkout_raw = raw_request.checkout
        currency_raw = raw_request.currency
        budget_raw = raw_request.budget
        rooms_raw = raw_request.rooms
    elif isinstance(raw_request, dict):
        city_raw = raw_request.get("city", raw_request.get("destination"))
        country = raw_request.get("country")
        checkin_raw = raw_request.get("checkin", raw_request.get("start_date"))
        checkout_raw = raw_request.get("checkout", raw_request.get("end_date"))
        currency_raw = raw_request.get("currency")
        budget_raw = raw_request.get("budget")
        rooms_raw = raw_request.get("rooms")
    else:
        raise TripPlanSessionError("PLAN_FIELDS", "Session request data missing.")

    destination = validate_city_name(city_raw, "destination")
    start_date = validate_date_str(checkin_raw, "start_date")
    end_date = validate_date_str(checkout_raw, "end_date")
    if start_date >= end_date:
        raise TripPlanSessionError("DATE", "end_date must be after start_date.")
    currency = validate_currency_code(currency_raw)

    # Calculate guests
    adults = 0
    children_list: List[int] = []
    normalized_rooms = []
    def add_party(r_adults, r_children):
        nonlocal adults
        if type(r_adults) is not int or r_adults <= 0:
            raise TripPlanSessionError("TRAVELLERS", "Each room requires a positive integer adult count.")
        if not isinstance(r_children, (list, tuple)) or any(
            type(age) is not int or not 0 <= age < 18 for age in r_children
        ):
            raise TripPlanSessionError("TRAVELLERS", "Every child age must be an integer from 0 to 17.")
        adults += r_adults
        children_list.extend(r_children)
        normalized_rooms.append((r_adults, tuple(sorted(r_children))))

    if rooms_raw is not None:
        if not isinstance(rooms_raw, (list, tuple)) or not rooms_raw:
            raise TripPlanSessionError("TRAVELLERS")
        for room in rooms_raw:
            if hasattr(room, "adults"):
                r_adults = room.adults
                r_children = getattr(room, "children", ())
            elif isinstance(room, dict):
                r_adults = room.get("adults", 0)
                r_children = room.get("children", [])
            else:
                raise TripPlanSessionError("TRAVELLERS")
            add_party(r_adults, r_children)
    elif isinstance(raw_request, dict) and "travellers" in raw_request:
        trav = raw_request["travellers"]
        if not isinstance(trav, dict):
            raise TripPlanSessionError("TRAVELLERS")
        add_party(trav.get("adults"), trav.get("children"))

    if adults <= 0:
        raise TripPlanSessionError("TRAVELLERS", "Trip must have at least 1 adult traveller.")

    children_tuple = tuple(sorted(children_list))

    # Origin validation
    clean_origin: Optional[str] = None
    if source_origin is not None:
        try:
            clean_origin = validate_city_name(source_origin, "origin")
        except TripPlanSessionError:
            clean_origin = None

    # Budget validation
    budget_amount_str: Optional[str] = None
    clean_budget_scope: str = "unspecified"
    budget_currency_str: str = currency

    if budget_raw is not None:
        if isinstance(budget_raw, dict):
            raw_amt = budget_raw.get("amount")
            raw_scope = budget_raw.get("scope", source_budget_scope)
            raw_curr = budget_raw.get("currency")
            if raw_curr is not None:
                budget_currency_str = validate_currency_code(raw_curr)
            if raw_amt is not None:
                budget_amount_str = format_cents_amount(raw_amt)
            if raw_scope in VALID_BUDGET_SCOPES:
                clean_budget_scope = raw_scope
            elif budget_amount_str is not None:
                clean_budget_scope = "unspecified"
        elif isinstance(budget_raw, str):
            budget_amount_str = format_cents_amount(budget_raw)
            if source_budget_scope in ("whole_trip", "hotel_only"):
                clean_budget_scope = source_budget_scope
            else:
                clean_budget_scope = "unspecified"

    if budget_amount_str is None:
        clean_budget_scope = "unspecified"

    return SessionContext(
        sid=str(sid),
        revision=revision,
        destination=destination,
        start_date=start_date,
        end_date=end_date,
        currency=currency,
        adults=adults,
        children=children_tuple,
        origin=clean_origin,
        budget_amount=budget_amount_str,
        budget_scope=clean_budget_scope,
        budget_currency=budget_currency_str,
        rooms=tuple(normalized_rooms),
        country=country,
        raw_budget=str(budget_raw) if budget_raw is not None else None,
    )


# --- Component Builders & Normalizers ---


def _verify_offer(base, offer, context, *, live_mode):
    """Withdraw unbound/stale prices. No trusted LIVE/archive adapter exists here.

    Caller-provided labels/timestamps are claims, not captured supplier evidence.
    MOCK amounts are explicitly synthetic; live_mode never validates them.
    Metadata is preserved so a rejected quote cannot masquerade as a new revision.
    """
    offer = offer or {}
    errors = []
    mode = base["source_mode"]
    for key in ("source_mode", "provider"):
        if key in offer and (not isinstance(offer[key], str) or not offer[key].strip()):
            errors.append("SOURCE_INVALID")
    if mode not in {"MOCK", "SYNTHETIC", "TEST", "SANDBOX", "LIVE", "ARCHIVE", "UNAVAILABLE"}:
        errors.append("SOURCE_UNKNOWN")
    for key in ("revision", "session_id", "start_date", "end_date", "travellers",
                "rooms", "origin", "destination", "source_observed_at", "expires_at",
                "capture_id", "source_ref", "synthetic", "source_note"):
        if key in offer:
            base[key] = copy.deepcopy(offer[key])
    required = mode in {"LIVE", "ARCHIVE"}
    bindings = {
        "revision": context.revision, "session_id": context.sid,
        "start_date": context.start_date, "end_date": context.end_date,
        "travellers": context.travellers_dict(),
        "rooms": context.rooms_dict(),
        "origin": context.origin, "destination": context.destination,
    }
    for key, expected in bindings.items():
        if key in offer or required:
            actual = offer.get(key)
            # bool == int is not valid party/revision evidence.
            equal = json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
            if not equal:
                errors.append("OFFER_" + key.upper() + "_MISMATCH")
    if required:
        for key in ("provider", "capture_id", "source_ref"):
            if not isinstance(offer.get(key), str) or not offer[key].strip():
                errors.append("SOURCE_EVIDENCE_MISSING")
        try:
            observed = datetime.fromisoformat(validate_offset_time_str(
                offer.get("source_observed_at"), "source_observed_at").replace("Z", "+00:00"))
            expires = datetime.fromisoformat(validate_offset_time_str(
                offer.get("expires_at"), "expires_at").replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if not observed <= now < expires or expires <= observed:
                errors.append("SOURCE_TIME_STALE")
        except TripPlanError:
            errors.append("SOURCE_TIME_UNKNOWN")
        # No amount is promoted solely by a dictionary claiming LIVE/ARCHIVE.
        errors.append("SOURCE_CAPTURE_UNVERIFIED")
    if live_mode and mode != "LIVE":
        errors.append("LIVE_FIRST_VIOLATION")
    status = offer.get("fare_status")
    if status is not None and status != "AVAILABLE":
        errors.append("OFFER_NOT_AVAILABLE")
    if base.get("synthetic") is True and mode in {"LIVE", "ARCHIVE"}:
        errors.append("SYNTHETIC_SOURCE_MISMATCH")
    if mode == "UNAVAILABLE":
        errors.append("SOURCE_UNAVAILABLE")
    if errors:
        base["amount"] = None
        base["fare_status"] = "STALE" if status == "STALE" else "UNKNOWN"
        base["verification_errors"] = sorted(set(errors))
    else:
        base["fare_status"] = "AVAILABLE" if base["amount"] is not None else "UNKNOWN"
    if mode in {"MOCK", "SYNTHETIC", "TEST", "SANDBOX"}:
        base["synthetic"] = True
    return base


def build_hotel_component(
    context: SessionContext,
    offer: Optional[Dict[str, Any]] = None,
    *,
    live_mode: bool = False,
) -> Dict[str, Any]:
    """Build normalized hotel component aligned with session dates."""
    if offer is not None and not isinstance(offer, dict):
        raise TripPlanSessionError("COMPONENT")
    base = {
        "name": f"Гостиница, {context.destination}",
        "checkin": context.start_date,
        "checkout": context.end_date,
        "amount": None,
        "currency": context.currency,
        "source_mode": "MOCK",
        "provider": "mock-hotel",
    }
    if offer:
        if "name" in offer and isinstance(offer["name"], str):
            base["name"] = offer["name"]
        if "checkin" in offer:
            base["checkin"] = validate_date_str(offer["checkin"], "hotel.checkin")
        if "checkout" in offer:
            base["checkout"] = validate_date_str(offer["checkout"], "hotel.checkout")
        if "currency" in offer:
            base["currency"] = validate_currency_code(offer["currency"])
        if "source_mode" in offer and isinstance(offer["source_mode"], str):
            base["source_mode"] = offer["source_mode"]
        if "provider" in offer and isinstance(offer["provider"], str):
            base["provider"] = offer["provider"]
        if "amount" in offer and offer["amount"] is not None:
            base["amount"] = format_cents_amount(offer["amount"])
        if "fare_status" in offer:
            base["fare_status"] = offer["fare_status"]

    # LIVE_FIRST: MOCK or ARCHIVE cannot be presented as current live price
    if live_mode and base["source_mode"] in MOCK_MODES:
        base["amount"] = None
        base["fare_status"] = "UNKNOWN"

    # Enforce date consistency
    if base["checkin"] != context.start_date or base["checkout"] != context.end_date:
        raise TripPlanSessionError("DATE", "Hotel checkin/checkout must match trip start/end date.")

    if base["amount"] is None:
        base["fare_status"] = base.get("fare_status") or "UNKNOWN"

    return _verify_offer(base, offer, context, live_mode=live_mode)


def build_transport_component(
    context: SessionContext,
    offer: Optional[Dict[str, Any]] = None,
    *,
    live_mode: bool = False,
) -> Dict[str, Any]:
    """Build normalized transport (flight) component. Never invent fake fares."""
    if offer is not None and not isinstance(offer, dict):
        raise TripPlanSessionError("COMPONENT")
    base = {
        "kind": "flight",
        "departure_at": None,
        "arrival_at": None,
        "return_departure_at": None,
        "return_arrival_at": None,
        "amount": None,
        "currency": context.currency,
        "source_mode": "MOCK",
        "provider": "mock-air",
    }
    if offer:
        for field in ("departure_at", "arrival_at", "return_departure_at", "return_arrival_at"):
            if field in offer and offer[field] is not None:
                base[field] = validate_offset_time_str(offer[field], f"transport.{field}")
        if "kind" in offer and isinstance(offer["kind"], str):
            base["kind"] = offer["kind"]
        if "currency" in offer:
            base["currency"] = validate_currency_code(offer["currency"])
        if "source_mode" in offer and isinstance(offer["source_mode"], str):
            base["source_mode"] = offer["source_mode"]
        if "provider" in offer and isinstance(offer["provider"], str):
            base["provider"] = offer["provider"]
        if "amount" in offer and offer["amount"] is not None:
            base["amount"] = format_cents_amount(offer["amount"])
        if "fare_status" in offer:
            base["fare_status"] = offer["fare_status"]

    # LIVE_FIRST: MOCK or ARCHIVE cannot be presented as current live price
    if live_mode and base["source_mode"] in MOCK_MODES:
        base["amount"] = None
        base["fare_status"] = "UNKNOWN"

    if base["amount"] is None:
        base["fare_status"] = base.get("fare_status") or "UNKNOWN"

    base = _verify_offer(base, offer, context, live_mode=live_mode)
    if base["departure_at"] is None or base["arrival_at"] is None:
        base["amount"] = None
        base["fare_status"] = "UNKNOWN"
    return base


def build_transfer_component(
    context: SessionContext,
    offer: Optional[Dict[str, Any]] = None,
    *,
    live_mode: bool = False,
) -> Dict[str, Any]:
    """Build normalized transfer component with strict one_way_arrival scope."""
    if offer is not None and not isinstance(offer, dict):
        raise TripPlanSessionError("COMPONENT")
    base = {
        "kind": "airport_hotel",
        "fare_scope": "one_way_arrival",
        "duration_minutes": None,
        "pickup_at": None,
        "amount": None,
        "currency": context.currency,
        "source_mode": "MOCK",
        "provider": "mock-transfer",
    }
    if offer:
        if "kind" in offer and isinstance(offer["kind"], str):
            base["kind"] = offer["kind"]
        if "fare_scope" in offer:
            if offer["fare_scope"] != "one_way_arrival":
                raise TripPlanSessionError("TRANSFER_SCOPE", "Only one_way_arrival is supported in TripPlan v0.")
        if "duration_minutes" in offer:
            dur = offer["duration_minutes"]
            if dur is not None and (type(dur) is not int or dur < 0):
                raise TripPlanSessionError("TRANSFER_DURATION", "duration_minutes must be non-negative integer or null.")
            base["duration_minutes"] = dur
        if "pickup_at" in offer and offer["pickup_at"] is not None:
            base["pickup_at"] = validate_offset_time_str(offer["pickup_at"], "transfer.pickup_at")
        if "currency" in offer:
            base["currency"] = validate_currency_code(offer["currency"])
        if "source_mode" in offer and isinstance(offer["source_mode"], str):
            base["source_mode"] = offer["source_mode"]
        if "provider" in offer and isinstance(offer["provider"], str):
            base["provider"] = offer["provider"]
        if "amount" in offer and offer["amount"] is not None:
            base["amount"] = format_cents_amount(offer["amount"])
        if "fare_status" in offer:
            base["fare_status"] = offer["fare_status"]

    # LIVE_FIRST: MOCK or ARCHIVE cannot be presented as current live price
    if live_mode and base["source_mode"] in MOCK_MODES:
        base["amount"] = None
        base["fare_status"] = "UNKNOWN"

    if base["amount"] is None:
        base["fare_status"] = base.get("fare_status") or "UNKNOWN"

    return _verify_offer(base, offer, context, live_mode=live_mode)


def build_insurance_component(
    context: SessionContext,
    offer: Optional[Dict[str, Any]] = None,
    *,
    live_mode: bool = False,
) -> Dict[str, Any]:
    """Build optional insurance component."""
    if offer is not None and not isinstance(offer, dict):
        raise TripPlanSessionError("COMPONENT")
    base = {
        "amount": None,
        "currency": context.currency,
        "source_mode": "MOCK",
        "provider": "mock-insurance",
        "fare_status": "UNKNOWN",
    }
    if offer:
        if "currency" in offer:
            base["currency"] = validate_currency_code(offer["currency"])
        if "source_mode" in offer and isinstance(offer["source_mode"], str):
            base["source_mode"] = offer["source_mode"]
        if "provider" in offer and isinstance(offer["provider"], str):
            base["provider"] = offer["provider"]
        if "amount" in offer and offer["amount"] is not None:
            base["amount"] = format_cents_amount(offer["amount"])
            base["fare_status"] = offer.get("fare_status", "AVAILABLE")
        elif "fare_status" in offer:
            base["fare_status"] = offer["fare_status"]

    if live_mode and base["source_mode"] in MOCK_MODES:
        base["amount"] = None
        base["fare_status"] = "UNKNOWN"

    if base["amount"] is None:
        base["fare_status"] = "UNKNOWN"

    return _verify_offer(base, offer, context, live_mode=live_mode)


# --- Core Session Assembler ---


class SessionTripPlanAssembler:
    """Safe session-aware assembler producing strictly valid TripPlan v0 documents."""

    @classmethod
    def assemble(
        cls,
        session_source: Any,
        *,
        origin: Optional[str] = None,
        budget_scope: Optional[str] = None,
        hotel: Optional[Dict[str, Any]] = None,
        transport: Optional[Dict[str, Any]] = None,
        transfer: Optional[Dict[str, Any]] = None,
        insurance: Optional[Dict[str, Any]] = None,
        expected_revision: Optional[int] = None,
        live_mode: bool = False,
        trip_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Assemble a canonical TripPlan dict from session context.

        Fails closed if critical context (origin, budget scope when budget specified)
        is unknown, or if expected_revision does not match.
        """
        context = extract_session_context(session_source, origin=origin, budget_scope=budget_scope,
                                          session_id=session_id)

        # Critical Context Checks (Fail-Closed)
        if not context.is_origin_known():
            raise TripPlanContextUnknownError(
                "ORIGIN_UNKNOWN",
                "Origin city is unknown; departure must be explicitly confirmed.",
                {"field": "origin"},
            )

        if context.budget_amount is not None and not context.is_budget_scope_known():
            raise TripPlanContextUnknownError(
                "BUDGET_SCOPE_UNKNOWN",
                "Budget scope is unknown; must specify whole_trip or hotel_only.",
                {"field": "budget.scope"},
            )

        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise TripPlanSessionError("REVISION")
        if expected_revision is not None and context.revision != expected_revision:
            raise StaleRevisionError(
                f"Session revision is {context.revision}, expected {expected_revision}.",
                {"current_revision": context.revision, "expected_revision": expected_revision},
            )

        # Build Components
        hotel_comp = build_hotel_component(context, hotel, live_mode=live_mode)
        transport_comp = build_transport_component(context, transport, live_mode=live_mode)
        transfer_comp = build_transfer_component(context, transfer, live_mode=live_mode)

        components = {
            "hotel": hotel_comp,
            "transport": transport_comp,
            "transfer": transfer_comp,
        }
        if insurance is not None:
            components["insurance"] = build_insurance_component(context, insurance, live_mode=live_mode)

        # Currency checks across budget and all components
        if context.budget_amount is not None and context.budget_currency != context.currency:
            raise TripPlanSessionError("MIXED_CURRENCY", f"Budget currency {context.budget_currency} must match trip currency {context.currency}.")

        for name, comp in components.items():
            if comp["currency"] != context.currency:
                raise TripPlanSessionError("MIXED_CURRENCY", f"{name} currency must match trip currency {context.currency}.")

        # Generate trip_id if not supplied
        effective_trip_id = f"session-{context.sid}-rev{context.revision}"
        if trip_id is not None and trip_id != effective_trip_id:
            raise TripPlanSessionError("SESSION_ID_MISMATCH")

        # Construct raw plan
        plan: Dict[str, Any] = {
            "trip_id": effective_trip_id,
            "session_id": context.sid,
            "revision": context.revision,
            "travellers": context.travellers_dict(),
            "origin": context.origin,
            "destination": context.destination,
            "start_date": context.start_date,
            "end_date": context.end_date,
            "currency": context.currency,
            "budget": context.budget_dict(),
            "hotel": hotel_comp,
            "transport": transport_comp,
            "transfer": transfer_comp,
        }
        if "insurance" in components:
            plan["insurance"] = components["insurance"]

        # Calculate exact total or fail to null
        total = Decimal("0.00")
        has_unknown_required = False
        for name in REQUIRED_COMPONENTS:
            amt = parse_amount(plan[name]["amount"])
            if amt is None:
                has_unknown_required = True
                break
            total += amt

        if has_unknown_required:
            plan["total_amount"] = None
        else:
            if "insurance" in plan and plan["insurance"]["amount"] is not None:
                total += parse_amount(plan["insurance"]["amount"])
            plan["total_amount"] = format(total, "f")

        # Compile Warnings
        warnings = []
        for name in REQUIRED_COMPONENTS + (("insurance",) if "insurance" in plan else ()):
            if plan[name]["amount"] is None:
                warnings.append({
                    "code": "PRICE_UNKNOWN",
                    "component": name,
                    "message": UNKNOWN_PRICE_MESSAGES[name],
                })

        # Schedule timing warnings
        warnings.extend(cls._check_schedule(plan))
        plan["warnings"] = warnings

        # Pass through canonical assemble_trip_plan to verify 100% contract compliance
        return assemble_trip_plan(plan, expected_revision=context.revision)

    @staticmethod
    def _check_schedule(plan: Dict[str, Any]) -> List[Dict[str, str]]:
        """Verify cross-component schedule timestamps (local offset instant comparisons)."""
        warnings = []
        arrival_str = plan["transport"].get("arrival_at")
        hotel_checkin_str = plan["hotel"].get("checkin")

        if arrival_str and hotel_checkin_str:
            arr_dt = datetime.fromisoformat(arrival_str.replace("Z", "+00:00"))
            checkin_date = date.fromisoformat(hotel_checkin_str)
            # Local arrival day based on the offset provided in arrival_at
            if arr_dt.date() > checkin_date:
                warnings.append({
                    "code": "CHECKIN_BEFORE_ARRIVAL",
                    "component": "hotel",
                    "message": "Заезд начинается раньше локальной даты прилёта. Такой стык нельзя считать подтверждённым.",
                })

        pickup_str = plan["transfer"].get("pickup_at")
        if pickup_str and arrival_str:
            pickup_dt = datetime.fromisoformat(pickup_str.replace("Z", "+00:00"))
            arr_dt = datetime.fromisoformat(arrival_str.replace("Z", "+00:00"))
            if pickup_dt < arr_dt:
                warnings.append({
                    "code": "TRANSFER_BEFORE_ARRIVAL",
                    "component": "transfer",
                    "message": "Трансфер начинается раньше фактического прилёта. Маршрут нельзя подтвердить.",
                })
        return warnings


# --- Rich Status Evaluator for Incomplete / Needs-Input Context ---


def _build_session_trip_plan_status(
    session_source: Any,
    *,
    origin: Optional[str] = None,
    budget_scope: Optional[str] = None,
    hotel: Optional[Dict[str, Any]] = None,
    transport: Optional[Dict[str, Any]] = None,
    transfer: Optional[Dict[str, Any]] = None,
    insurance: Optional[Dict[str, Any]] = None,
    expected_revision: Optional[int] = None,
    live_mode: bool = False,
) -> Dict[str, Any]:
    """Inspect session and return detailed structured status without throwing.

    Clearly surfaces missing fields, questions, provider availability, and budget fit.
    """
    missing_fields: List[str] = []
    questions: List[str] = []
    warnings: List[str] = []

    try:
        context = extract_session_context(session_source, origin=origin, budget_scope=budget_scope)
    except TripPlanSessionError as err:
        return {
            "status": "error",
            "verification": None,
            "error_code": err.code,
            "message": err.message,
            "plan": None,
            "total_amount": None,
            "missing_fields": ["session_context"],
            "questions": ["Пожалуйста, укажите параметры поездки (город назначения и даты)."],
            "warnings": [err.code],
            "budget_status": None,
        }

    # Revision check
    is_stale = expected_revision is not None and context.revision != expected_revision
    if is_stale:
        warnings.append("STALE_REVISION")

    # Missing Context Checks
    if not context.is_origin_known():
        missing_fields.append("origin")
        questions.append("Из какого города вы вылетаете?")

    if context.budget_amount is not None and not context.is_budget_scope_known():
        missing_fields.append("budget_scope")
        questions.append("Уточните область бюджета: на всю поездку (whole_trip) или только на отель (hotel_only)?")

    # Provider pricing summary
    hotel_comp = build_hotel_component(context, hotel, live_mode=live_mode)
    transport_comp = build_transport_component(context, transport, live_mode=live_mode)
    transfer_comp = build_transfer_component(context, transfer, live_mode=live_mode)
    if insurance is not None:
        build_insurance_component(context, insurance, live_mode=live_mode)
    if is_stale:
        for comp in (hotel_comp, transport_comp, transfer_comp):
            comp["amount"] = None
            comp["fare_status"] = "STALE"

    provider_summary = {
        "hotel": {
            "available": hotel_comp["amount"] is not None,
            "source_mode": hotel_comp["source_mode"],
            "provider": hotel_comp["provider"],
            "amount": hotel_comp["amount"],
        },
        "transport": {
            "available": transport_comp["amount"] is not None,
            "source_mode": transport_comp["source_mode"],
            "provider": transport_comp["provider"],
            "amount": transport_comp["amount"],
        },
        "transfer": {
            "available": transfer_comp["amount"] is not None,
            "source_mode": transfer_comp["source_mode"],
            "provider": transfer_comp["provider"],
            "amount": transfer_comp["amount"],
        },
    }

    if not transport_comp["amount"]:
        warnings.append("FLIGHT_PRICE_UNAVAILABLE")
        if not any("перелёт" in q for q in questions):
            questions.append("Тарифы на перелёт не подтверждены живыми провайдерами.")

    if not transfer_comp["amount"]:
        warnings.append("TRANSFER_PRICE_UNAVAILABLE")

    # Try assembling if no missing critical fields
    plan = None
    b_status = None
    if not missing_fields and not is_stale:
        try:
            plan = SessionTripPlanAssembler.assemble(
                session_source,
                origin=origin,
                budget_scope=budget_scope,
                hotel=hotel,
                transport=transport,
                transfer=transfer,
                insurance=insurance,
                expected_revision=expected_revision,
                live_mode=live_mode,
            )
            b_status = budget_status(plan)
            status = "ready" if plan["total_amount"] is not None else "needs_input"
            verification = "VERIFIED_CONTEXT"
        except TripPlanSessionError as err:
            raise err
    elif is_stale:
        status = "stale"
        verification = "STALE_REVISION"
    else:
        status = "needs_input"
        verification = "UNVERIFIED"

    return {
        "status": status,
        "verification": verification,
        "plan": plan,
        "total_amount": plan["total_amount"] if plan else None,
        "missing_fields": missing_fields,
        "questions": questions,
        "warnings": warnings,
        "budget_status": b_status,
        "provider_summary": provider_summary,
    }


def build_session_trip_plan_status(session_source: Any, **kwargs) -> Dict[str, Any]:
    """Return typed validation failures from context, builders and final assembly."""
    try:
        return _build_session_trip_plan_status(session_source, **kwargs)
    except TripPlanError as err:
        return {
            "status": "error", "verification": None, "error_code": err.code,
            "message": getattr(err, "message", err.code), "plan": None,
            "total_amount": None, "missing_fields": [], "questions": [],
            "warnings": [err.code], "budget_status": None,
        }


# --- Standalone Grounded Offline Demo Cases ---


def create_grounded_dubai_case() -> Dict[str, Any]:
    """Complete Moscow -> Dubai 12-19 Nov 2026 fixture (from docs/contracts/trip_plan_v0.json).

    2 adults, 1 child age 4, 150000.00 RUB whole_trip budget.
    Exact arithmetic: 81000 + 54000 + 5500 = 140500.00 RUB.
    """
    session = {
        "sid": "grounded-dubai-demo-sid-12345678",
        "revision": 0,
        "destination": "Дубай",
        "start_date": "2026-11-12",
        "end_date": "2026-11-19",
        "currency": "RUB",
        "origin": "Москва",
        "budget": {"amount": "150000.00", "currency": "RUB", "scope": "whole_trip"},
        "travellers": {"adults": 2, "children": [4]},
    }
    hotel = {
        "name": "Демо-отель, Дубай",
        "checkin": "2026-11-12",
        "checkout": "2026-11-19",
        "amount": "81000.00",
        "currency": "RUB",
        "source_mode": "MOCK",
        "provider": "mock-hotel",
    }
    transport = {
        "kind": "flight",
        "departure_at": "2026-11-12T10:00:00+03:00",
        "arrival_at": "2026-11-12T16:30:00+04:00",
        "return_departure_at": "2026-11-19T17:40:00+04:00",
        "return_arrival_at": "2026-11-19T22:05:00+03:00",
        "amount": "54000.00",
        "currency": "RUB",
        "source_mode": "MOCK",
        "provider": "mock-air",
    }
    transfer = {
        "fare_scope": "one_way_arrival",
        "duration_minutes": None,
        "pickup_at": None,
        "amount": "5500.00",
        "currency": "RUB",
        "source_mode": "MOCK",
        "provider": "mock-transfer",
    }
    insurance = {
        "amount": None,
        "currency": "RUB",
        "source_mode": "MOCK",
        "provider": "mock-insurance",
        "fare_status": "UNKNOWN",
    }
    return SessionTripPlanAssembler.assemble(
        session,
        origin="Москва",
        budget_scope="whole_trip",
        hotel=hotel,
        transport=transport,
        transfer=transfer,
        insurance=insurance,
    )


def create_grounded_kazan_case() -> Dict[str, Any]:
    """Kazan -> Dubai 12-19 Nov 2026 case with hotel_only budget scope."""
    session = {
        "sid": "grounded-kazan-demo-sid-87654321",
        "revision": 0,
        "destination": "Дубай",
        "start_date": "2026-11-12",
        "end_date": "2026-11-19",
        "currency": "RUB",
        "origin": "Казань",
        "budget": {"amount": "90000.00", "currency": "RUB", "scope": "hotel_only"},
        "travellers": {"adults": 2, "children": [4]},
    }
    hotel = {
        "name": "Beach Resort, Dubai",
        "checkin": "2026-11-12",
        "checkout": "2026-11-19",
        "amount": "81000.00",
        "currency": "RUB",
        "source_mode": "MOCK",
        "provider": "mock-hotel",
    }
    transport = {
        "kind": "flight",
        "departure_at": "2026-11-12T09:00:00+03:00",
        "arrival_at": "2026-11-12T15:30:00+04:00",
        "amount": "56000.00",
        "currency": "RUB",
        "source_mode": "MOCK",
        "provider": "mock-air",
    }
    transfer = {
        "fare_scope": "one_way_arrival",
        "duration_minutes": 45,
        "pickup_at": "2026-11-12T16:15:00+04:00",
        "amount": "6000.00",
        "currency": "RUB",
        "source_mode": "MOCK",
        "provider": "mock-transfer",
    }
    return SessionTripPlanAssembler.assemble(
        session,
        origin="Казань",
        budget_scope="hotel_only",
        hotel=hotel,
        transport=transport,
        transfer=transfer,
    )


def create_offline_unpriced_paris_case(*, live_mode: bool = False) -> Dict[str, Any]:
    """A01 input: Moscow -> Paris, 1 adult, 80000 RUB whole_trip budget.

    Synthetic MOCK scenario, with no supplier capture or current availability.
    The 38000 hotel value is test data. Flights and transfers have no schedule
    or price. live_mode withdraws every synthetic amount.
    """
    session = {
        "sid": "offline-paris-sid-11223344",
        "revision": 0,
        "destination": "Париж",
        "start_date": "2026-11-12",
        "end_date": "2026-11-15",
        "currency": "RUB",
        "origin": "Москва",
        "budget": {"amount": "80000.00", "currency": "RUB", "scope": "whole_trip"},
        "travellers": {"adults": 1, "children": []},
    }
    hotel = {
        "name": "Paris Latin Quarter Hotel",
        "checkin": "2026-11-12",
        "checkout": "2026-11-15",
        "amount": "38000.00",
        "currency": "RUB",
        "source_mode": "MOCK",
        "provider": "mock-hotel",
        "synthetic": True,
        "source_note": "Synthetic test fixture; no supplier capture or availability.",
    }
    # Unpriced transport and transfer
    transport = {
        "kind": "flight",
        "amount": None,
        "currency": "RUB",
        "source_mode": "UNAVAILABLE",
        "provider": "none",
        "fare_status": "UNKNOWN",
    }
    transfer = {
        "fare_scope": "one_way_arrival",
        "duration_minutes": None,
        "pickup_at": None,
        "amount": None,
        "currency": "RUB",
        "source_mode": "UNAVAILABLE",
        "provider": "none",
        "fare_status": "UNKNOWN",
    }
    return SessionTripPlanAssembler.assemble(
        session,
        origin="Москва",
        budget_scope="whole_trip",
        hotel=hotel,
        transport=transport,
        transfer=transfer,
        live_mode=live_mode,
    )


def create_offline_stale_dates_case(initial_plan: Dict[str, Any], days: int = 2) -> Dict[str, Any]:
    """Shift dates of an assembled plan, invalidating dated prices to null.

    Advances revision and marks prices as STALE without inventing new fares.
    """
    plan = copy.deepcopy(initial_plan)
    delta = timedelta(days=days)
    new_start = (date.fromisoformat(plan["start_date"]) + delta).isoformat()
    new_end = (date.fromisoformat(plan["end_date"]) + delta).isoformat()

    plan["start_date"] = new_start
    plan["end_date"] = new_end
    plan["revision"] += 1
    plan["hotel"]["checkin"] = new_start
    plan["hotel"]["checkout"] = new_end

    # Invalidate dated prices
    for name in REQUIRED_COMPONENTS:
        plan[name]["amount"] = None
        plan[name]["fare_status"] = "STALE"

    plan["total_amount"] = None
    plan["warnings"] = [
        {
            "code": "PRICE_UNKNOWN",
            "component": name,
            "message": UNKNOWN_PRICE_MESSAGES[name],
        }
        for name in REQUIRED_COMPONENTS
    ]
    plan["warnings"].append({
        "code": "STALE_REVISION",
        "component": "transport",
        "message": "Ревизия изменилась. Цены отеля, перелёта и трансфера сняты. Это не новый тариф.",
    })
    return plan


# --- Server Endpoint Integration Helper ---


def read_session_trip_plan(store, owner, sid, *, expected_revision):
    """HTTP read boundary: only stored context, never client evidence or prices.

    No trusted supplier capture adapter is registered in this build. In particular,
    saved dictionaries with source=LIVE, capture IDs, timestamps or verified flags
    are still claims. Do not pass them to A05's pure fixture evaluator. Enabling
    real A05 requires a server-owned capture with independently known authority,
    owner/session/current-revision binding and observation/expiry timestamps.
    """
    def unknown(code, fields, questions, *, context=None):
        return 400, {
            "error": "TRIP_PLAN_CONTEXT_UNKNOWN", "code": code,
            "status": "NEEDS_INPUT", "verification": "UNVERIFIED",
            "missing_fields": fields, "questions": questions,
            "reasons": ["No trusted current supplier capture is available."],
            "plan": None, "total_amount": None,
            "session_id": sid, "revision": expected_revision,
            "context": context,
            "overnight": {"status": "NEEDS_INPUT", "is_feasible": False,
                          "evaluated": False, "reason": "TRUSTED_CAPTURE_UNAVAILABLE"},
        }

    if not isinstance(owner, str) or not owner.strip():
        return 403, {"error": "CAPABILITY_NOT_OWNED"}
    if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{32}", sid):
        return 400, {"error": "SESSION_ID_INVALID"}
    if type(expected_revision) is not int or not 0 <= expected_revision <= 2**53 - 1:
        return 400, {"error": "REVISION_REQUIRED"}
    if (not isinstance(getattr(store, "db", None), sqlite3.Connection)
            or not callable(getattr(store, "transaction", None))):
        return 403, {"error": "OWNER_AUTHORITY_UNSUPPORTED"}
    try:
        with store.transaction():
            if store.db.execute("SELECT owner FROM session_owners WHERE id=?", (sid,)).fetchone() != (owner,):
                return 403, {"error": "CAPABILITY_NOT_OWNED"}
            saved = store.get_session(sid)
            if not isinstance(saved, dict) or saved.get("schema") != 1:
                return 400, {"error": "SESSION_UNVERIFIED"}
            state = saved.get("state")
            if not isinstance(state, dict) or type(state.get("revision")) is not int:
                return 400, {"error": "REVISION_REQUIRED"}
            if expected_revision != state["revision"]:
                return 409, {"error": "STALE_REVISION"}
            req = state.get("request")
            if not isinstance(req, dict):
                return 400, {"error": "SESSION_UNVERIFIED"}
            context = extract_session_context(saved, session_id=sid)
            fields, questions = [], []
            if not context.is_origin_known():
                fields.append("origin")
                questions.append("Из какого города вы вылетаете? Укажите его в условиях поездки.")
            if "budget" not in req or (req["budget"] is not None and context.budget_amount is None):
                fields.append("budget.amount")
                questions.append("Какой бюджет и валюта поездки? Укажите сумму или явно отсутствие лимита.")
            if not context.is_budget_scope_known():
                fields.append("budget.scope")
                questions.append("Этот бюджет на отель или на всю поездку? Подтвердите область бюджета отдельно.")
            # #136 owns separate hotel/whole-trip limits. Expose both verbatim;
            # never substitute the hotel amount for a confirmed whole-trip limit.
            from scoped_budget import public_budget_scopes
            public_context = {
                "origin": context.origin, "destination": context.destination,
                "start_date": context.start_date, "end_date": context.end_date,
                "travellers": context.travellers_dict(), "currency": context.currency,
                "budget": context.budget_dict(),
                "budget_scopes": public_budget_scopes(req, saved.get("budget_scopes")),
            }
            if fields:
                code = "ORIGIN_UNKNOWN" if "origin" in fields else "BUDGET_UNKNOWN"
                return unknown(code, fields, questions, context=public_context)
            return unknown("TRUSTED_CAPTURE_UNAVAILABLE",
                ["transport.arrival_at", "hotel.late_checkin_policy", "supplier.capture"],
                ["Нужно время прибытия с часовым поясом из подтверждённого источника перевозчика.",
                 "Нужны действующие правила позднего заселения из подтверждённого источника отеля. "
                 "Свяжитесь с отелем; пока заселение не подтверждено."], context=public_context)
    except TripPlanError as error:
        return 400, {"error": error.code, "status": "NEEDS_INPUT", "plan": None}
    except sqlite3.Error:
        return 403, {"error": "OWNER_AUTHORITY_UNAVAILABLE"}


def handle_session_trip_plan_request(
    store: Any,
    owner: str,
    sid: str,
    *,
    expected_revision: Optional[int] = None,
    origin: Optional[str] = None,
    budget_scope: Optional[str] = None,
    live_mode: bool = False,
) -> Tuple[int, Dict[str, Any]]:
    """Internal fixture assembler; not the authenticated HTTP entry point.

    HTTP uses read_session_trip_plan and never accepts these context overrides.
    This helper preserves the offline assembler contract for internal callers:
    - Verifies session ownership and existence
    - Extracts context from SQLite session record
    - Assembles TripPlan using SessionTripPlanAssembler
    - Returns (200, plan) on success
    - Returns (400, {error, message, details}) on fail-closed validation failure
    """
    if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{32}", sid):
        return 400, {"error": "DEMO_SESSION_REQUIRED", "message": "Invalid sid format."}

    if not isinstance(owner, str) or not owner.strip():
        return 403, {"error": "CAPABILITY_NOT_OWNED"}
    if (not isinstance(getattr(store, "db", None), sqlite3.Connection)
            or not callable(getattr(store, "get_session", None))
            or not callable(getattr(store, "transaction", None))):
        return 403, {"error": "OWNER_AUTHORITY_UNSUPPORTED"}
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        return 400, {"error": "REVISION"}
    try:
        # Ownership, record and revision are one SQLite snapshot. No legacy load.
        with store.transaction():
            owner_row = store.db.execute(
                "SELECT owner FROM session_owners WHERE id=?", (sid,)).fetchone()
            if owner_row != (owner,):
                return 403, {"error": "CAPABILITY_NOT_OWNED"}
            saved = store.get_session(sid)
            if not isinstance(saved, dict) or saved.get("schema") != 1:
                return 400, {"error": "DEMO_SESSION_UNVERIFIED"}
            state = saved.get("state")
            if not isinstance(state, dict) or type(state.get("revision")) is not int:
                return 400, {"error": "REVISION"}
            if expected_revision is not None and expected_revision != state["revision"]:
                return 409, {"error": "STALE_REVISION"}
            plan = SessionTripPlanAssembler.assemble(
                saved, session_id=sid, origin=origin, budget_scope=budget_scope,
                expected_revision=expected_revision, live_mode=live_mode,
            )
        return 200, plan
    except TripPlanError as err:
        return 400, {"error": err.code, "message": getattr(err, "message", err.code)}
    except sqlite3.Error:
        return 403, {"error": "OWNER_AUTHORITY_UNAVAILABLE"}
