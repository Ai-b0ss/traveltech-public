"""Normalized TransferObservation and error models for airport transfer sources.

Principles:
1. Unknown not zero: null amount represents an unverified or unknown price, never 0.00.
2. Historical snapshot not live: data from research files must be marked OFFLINE_SNAPSHOT.
3. Planner fare not checkout: quotes are preliminary estimates, checkout_verified is always False.
4. Input validation and bounded types: strict decimal representation, defense against injection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from .sanitizer import sanitize_text

DECIMAL_PRICE_PATTERN = re.compile(r"^\d+\.\d{2}$")


class TransferError(Exception):
    """Normalized domain error for airport transfer adapters."""

    def __init__(self, code: str, http_status: int | None = None, details: str | None = None):
        self.code = code
        self.http_status = http_status
        self.details = details
        msg = f"[{code}]"
        if http_status is not None:
            msg += f" (HTTP {http_status})"
        if details:
            msg += f": {details}"
        super().__init__(msg)


class TransferDisambiguationError(TransferError):
    """Raised when origin/destination query returns disambiguation options (e.g. HTTP 300)."""

    def __init__(self, message: str, choices: list[dict[str, Any]] | None = None):
        super().__init__("DISAMBIGUATION_REQUIRED", http_status=300, details=message)
        self.choices = choices or []


class SnapshotDateMismatchError(TransferError):
    """Raised when requested travel date does not match offline snapshot service date."""

    def __init__(self, details: str | None = None):
        super().__init__("SNAPSHOT_DATE_MISMATCH", details=details)


@dataclass(frozen=True)
class TransferWarning:
    """Warning record associated with a transfer observation."""
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class TransferObservation:
    """Normalized transfer observation contract.

    Compatible with Igor's TripPlan v0 specification (docs/contracts/TRIP_PLAN_V0.md).
    """

    origin_stop_id: str
    origin_name: str
    destination_stop_id: str
    destination_name: str
    departure_local: str | None
    arrival_local: str | None
    duration_minutes: int | None
    timezone_name: str
    utc_offset: str
    mode: str
    direction: str
    operator: str
    route_name: str | None
    source_provider: str
    observed_at: str
    fare_amount: str | None
    currency: str | None
    fare_basis: str | None
    price_status: str
    fare_source: str
    source_mode: str
    license_attribution: str
    checkout_verified: bool = False
    warnings: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # 1. checkout_verified must strictly be False
        if self.checkout_verified is not False:
            raise ValueError("checkout_verified must always be False for transfer observations")

        # 2. Source mode validation
        if self.source_mode not in ("LIVE", "OFFLINE_SNAPSHOT", "MOCK"):
            raise ValueError(f"Invalid source_mode: {self.source_mode}")

        # 3. Fare amount validation: strictly string or None, never bool or float
        if self.fare_amount is not None:
            if not isinstance(self.fare_amount, str):
                raise ValueError("fare_amount must be a Decimal string or None")
            if not DECIMAL_PRICE_PATTERN.fullmatch(self.fare_amount):
                raise ValueError(f"fare_amount must be formatted as '0.00', got: {self.fare_amount}")
            try:
                dec = Decimal(self.fare_amount)
            except InvalidOperation:
                raise ValueError(f"Invalid decimal fare_amount: {self.fare_amount}")
            if dec < 0:
                raise ValueError("fare_amount cannot be negative")

            # Zero fare is only allowed if policy explicitly states POLICY_FREE
            if dec == Decimal("0.00"):
                if self.price_status != "POLICY_FREE":
                    raise ValueError(
                        "Zero fare amount (0.00) is only permitted when price_status is POLICY_FREE"
                    )
        else:
            # If fare is None, price_status must be UNKNOWN
            if self.price_status != "UNKNOWN":
                raise ValueError(
                    f"When fare_amount is None, price_status must be UNKNOWN, got: {self.price_status}"
                )

        # 4. Defensive text sanitization check
        # Verify that strings do not contain active injection or script tags
        for field_name in ("origin_name", "destination_name", "operator", "route_name"):
            val = getattr(self, field_name)
            if val is not None and ("<script" in val.lower() or "<iframe" in val.lower()):
                raise ValueError(f"Unsanitized HTML detected in {field_name}")

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return {
            "origin_stop_id": self.origin_stop_id,
            "origin_name": self.origin_name,
            "destination_stop_id": self.destination_stop_id,
            "destination_name": self.destination_name,
            "departure_local": self.departure_local,
            "arrival_local": self.arrival_local,
            "duration_minutes": self.duration_minutes,
            "timezone_name": self.timezone_name,
            "utc_offset": self.utc_offset,
            "mode": self.mode,
            "direction": self.direction,
            "operator": self.operator,
            "route_name": self.route_name,
            "source_provider": self.source_provider,
            "observed_at": self.observed_at,
            "fare_amount": self.fare_amount,
            "currency": self.currency,
            "fare_basis": self.fare_basis,
            "price_status": self.price_status,
            "fare_source": self.fare_source,
            "source_mode": self.source_mode,
            "license_attribution": self.license_attribution,
            "checkout_verified": self.checkout_verified,
            "warnings": list(self.warnings),
        }

    def to_trip_plan_transfer(self) -> dict[str, Any]:
        """Convert to Igor's TripPlan v0 transfer component format."""
        slug = re.sub(r"[^a-z0-9]+", "-", self.operator.lower()).strip("-")
        return {
            "kind": "airport_transfer",
            "fare_scope": (
                "one_way_arrival"
                if "inbound" in self.direction.lower() or "airport_to_city" in self.direction.lower()
                else "one_way_departure"
            ),
            "duration_minutes": self.duration_minutes,
            "pickup_at": self.departure_local,
            "amount": self.fare_amount,
            "currency": self.currency,
            "source_mode": self.source_mode,
            "provider": slug,
        }

    def to_trip_plan_warnings(self) -> list[dict[str, str]]:
        """Extract warnings formatted for TripPlan v0 warnings array."""
        result = []
        for w in self.warnings:
            result.append(
                {
                    "code": w.get("code", "TRANSFER_WARNING"),
                    "component": "transfer",
                    "message": w.get("message", ""),
                }
            )
        if not any(w["code"] == "LAST_MILE_NOT_INCLUDED" for w in result):
            result.append(
                {
                    "code": "LAST_MILE_NOT_INCLUDED",
                    "component": "transfer",
                    "message": "Airport transfer arrives at transit station; hotel last-mile transfer is not included in fare",
                }
            )
        return result
