"""Isolated Ignav one-way client; past authenticated fare smoke is documented in research.

Contract: https://ignav.com/docs/one-way and /docs/response-format (2026-10-24).
No booking endpoints, retries, logging, credential persistence, or app integration.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
import json
import os
import re
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ENDPOINT = "https://ignav.com/api/fares/one-way"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
Transport = Callable[[dict, dict, float], tuple[int, bytes]]


class IgnavError(Exception):
    """Only fixed, non-secret classifications cross the client boundary."""

    def __init__(self, code: str, http_status: int | None = None):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_transport(payload: dict, headers: dict, timeout: float) -> tuple[int, bytes]:
    request = Request(ENDPOINT, json.dumps(payload).encode(), headers, method="POST")
    try:
        try:
            with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
                status, body = response.status, response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            with error:
                status, body = error.code, error.read(MAX_RESPONSE_BYTES + 1)
    except (URLError, TimeoutError, OSError, HTTPException):
        raise IgnavError("UNKNOWN_OUTCOME") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise IgnavError("RESPONSE_TOO_LARGE", status)
    return status, body


def utc_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timezone required")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class FlightSegment:
    """Allowlisted, non-booking segment metadata; missing means unknown."""
    departure_airport: str | None
    arrival_airport: str | None
    marketing_carrier_code: str | None
    flight_number: str | None
    operating_carrier_name: str | None


def optional_code(value, pattern: str, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Invalid " + label)
    return value


def baggage_count(value) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 20:
        raise ValueError("Invalid baggage count")
    return value


@dataclass(frozen=True)
class FlightOption:
    amount: Decimal
    currency: str
    price_status: str
    segment_count: int
    departure_utc: datetime | None
    arrival_utc: datetime | None
    segments: tuple[FlightSegment, ...] = ()
    departure_airport: str | None = None
    arrival_airport: str | None = None
    checked_bags: int | None = None
    carry_on_bags: int | None = None
    requires_self_transfer: bool | None = None
    carrier: str | None = None


@dataclass(frozen=True)
class SearchResult:
    offers: tuple[FlightOption, ...]
    itinerary_count: int
    observed_at: datetime | None
    received_at: datetime
    status: str
    # No documented cache/expiry signal: never infer these from HTTP 200.
    cache_status: str = "UNKNOWN"
    ttl_seconds: None = None


class FlightSearchProvider:
    def __init__(self, api_key: str, *, transport: Transport = http_transport):
        if not api_key or not re.fullmatch(r"[!-~]{1,8192}", api_key):
            raise IgnavError("API_KEY_REQUIRED")
        self._api_key = api_key
        self._transport = transport

    @classmethod
    def from_environment(cls, *, transport: Transport = http_transport):
        return cls(os.environ.get("IGNAV_API_KEY", ""), transport=transport)

    def search_one_way(
        self, origin: str, destination: str, departure_date: str, *, market: str = "US"
    ) -> SearchResult:
        payload = one_way_payload(origin, destination, departure_date, market)
        status, body = self._transport(
            payload,
            {"Content-Type": "application/json", "X-Api-Key": self._api_key},
            60.0,
        )
        if status != 200:
            code = {
                401: "AUTH_BLOCKED",
                403: "EMAIL_NOT_VERIFIED",
                402: "BILLING_REQUIRED",
                424: "UPSTREAM_FAILED",
                429: "LIMIT_REACHED",
            }.get(status, "HTTP_ERROR")
            raise IgnavError(code, status)
        try:
            data = json.loads(body, parse_float=Decimal)
            if not isinstance(data, dict) or not isinstance(data["itineraries"], list):
                raise ValueError("Invalid response")
            for field in ("origin", "destination", "departure_date"):
                if data.get(field) != payload[field]:
                    raise ValueError("Route mismatch")
            observed = utc_timestamp(data["observed_at"])
            offers = []
            for itinerary in data["itineraries"]:
                price = itinerary["price"]
                if isinstance(price["amount"], bool):
                    raise ValueError("Invalid price")
                amount = Decimal(str(price["amount"]))
                if not amount.is_finite() or amount < 0:
                    raise ValueError("Invalid price")
                currency, price_status = price["currency"], price["status"]
                if not re.fullmatch(r"[A-Z]{3}", currency):
                    raise ValueError("Invalid currency")
                if price_status not in {"verified", "unverified"}:
                    raise ValueError("Unknown price status")
                segments = itinerary["outbound"]["segments"]
                if not isinstance(segments, list):
                    raise ValueError("Invalid segments")
                if not amount or not segments:
                    # Manual link placeholders are not fares.
                    continue
                safe_segments = []
                for segment in segments:
                    if not isinstance(segment, dict):
                        raise ValueError("Invalid segment")
                    safe_segments.append(
                        FlightSegment(
                            optional_code(segment.get("departure_airport"), r"[A-Z]{3}", "departure airport"),
                            optional_code(segment.get("arrival_airport"), r"[A-Z]{3}", "arrival airport"),
                            optional_code(segment.get("marketing_carrier_code"), r"[A-Z0-9]{2}", "carrier"),
                            optional_code(segment.get("flight_number"), r"[A-Za-z0-9]{1,16}", "flight number"),
                            optional_code(segment.get("operating_carrier_name"), r"[^\x00-\x1f]{1,128}", "carrier name"),
                        )
                    )
                bags = itinerary.get("bags") or {}
                if not isinstance(bags, dict):
                    raise ValueError("Invalid bags")
                self_transfer = itinerary.get("requires_self_transfer")
                if self_transfer is not None and type(self_transfer) is not bool:
                    raise ValueError("Invalid self transfer")
                leg_carrier = itinerary["outbound"].get("carrier")
                if leg_carrier is not None:
                    leg_carrier = optional_code(leg_carrier, r"[^\x00-\x1f]{1,128}", "leg carrier")
                offers.append(
                    FlightOption(
                        amount,
                        currency,
                        price_status,
                        len(segments),
                        utc_timestamp(segments[0]["departure_time_utc"]),
                        utc_timestamp(segments[-1]["arrival_time_utc"]),
                        segments=tuple(safe_segments),
                        departure_airport=safe_segments[0].departure_airport,
                        arrival_airport=safe_segments[-1].arrival_airport,
                        checked_bags=baggage_count(bags.get("checked")),
                        carry_on_bags=baggage_count(bags.get("carry_on")),
                        requires_self_transfer=self_transfer,
                        carrier=leg_carrier,
                    )
                )
            return SearchResult(
                tuple(offers),
                len(data["itineraries"]),
                observed,
                datetime.now(timezone.utc),
                "LIVE_SEARCH" if offers else "EMPTY",
            )
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, InvalidOperation):
            raise IgnavError("INVALID_RESPONSE", status) from None


def one_way_payload(origin: str, destination: str, departure_date: str, market: str) -> dict:
    if not all(re.fullmatch(r"[A-Z]{3}", code) for code in (origin, destination)):
        raise IgnavError("INVALID_REQUEST")
    if origin == destination or not re.fullmatch(r"[A-Z]{2}", market):
        raise IgnavError("INVALID_REQUEST")
    try:
        parsed = date.fromisoformat(departure_date)
        if parsed.isoformat() != departure_date or parsed < datetime.now(timezone.utc).date():
            raise ValueError("Date must be future or today")
    except (TypeError, ValueError):
        raise IgnavError("INVALID_REQUEST") from None
    return {
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "adults": 1,
        "market": market,
    }
