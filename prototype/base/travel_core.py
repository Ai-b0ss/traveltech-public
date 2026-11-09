"""Offline research core. No LLM, HTTP, booking, or payment implementation.

Only validates normalized data; passing constraints never confirms inventory.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol
import json, re, time

CURRENCIES = {"RUB", "CNY", "USD", "EUR", "GBP", "JPY", "KWD"}
DIGITS = {c: 2 for c in CURRENCIES}
DIGITS.update(JPY=0, KWD=3)
MONEY = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?\Z", re.ASCII)


class InputError(ValueError):
    pass


class SourceError(ValueError):
    pass


def money(x):
    if not isinstance(x, str) or not MONEY.fullmatch(x):
        raise InputError("INVALID_MONEY")
    return Decimal(x)


def currency_money(x, currency):
    value = money(x)
    if (value * (10 ** DIGITS[currency])) != (value * (10 ** DIGITS[currency])).to_integral_value():
        raise InputError("FRACTIONAL_MINOR_UNIT")
    return value


def day(x):
    if not isinstance(x, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", x):
        raise InputError("INVALID_DATE")
    try:
        return date.fromisoformat(x)
    except ValueError as e:
        raise InputError("INVALID_DATE") from e


def clock(x):
    if not isinstance(x, datetime) or x.tzinfo is None or x.utcoffset() is None:
        raise InputError("AWARE_TIMESTAMP_REQUIRED")
    return x


def safe_json(raw):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise InputError("DUPLICATE_KEY")
            d[k] = v
        return d

    def constant(x):
        raise InputError("NONFINITE_NUMBER")

    try:
        encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
        if not isinstance(encoded, bytes):
            raise InputError("INVALID_JSON_INPUT")
        if len(encoded) > 1048576:
            raise InputError("JSON_TOO_LARGE")
        value = json.loads(
            encoded.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
        stack = [(value, 0)]
        while stack:
            item, depth = stack.pop()
            if depth > 32:
                raise InputError("JSON_TOO_DEEP")
            if isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, dict):
                for k, v in item.items():
                    k.encode("utf-8")
                    stack.append((v, depth + 1))
            elif isinstance(item, list):
                stack.extend((v, depth + 1) for v in item)
        return value
    except (UnicodeError, RecursionError, json.JSONDecodeError) as e:
        raise InputError("INVALID_JSON") from e


@dataclass(frozen=True)
class Guests:
    adults: int
    children: tuple[int, ...] = ()

    def validate(self):
        if type(self.adults) is not int or not 1 <= self.adults <= 20:
            raise InputError("INVALID_ADULTS")
        if (
            not isinstance(self.children, tuple)
            or len(self.children) > 16
            or any(type(a) is not int or not 0 <= a <= 17 for a in self.children)
        ):
            raise InputError("INVALID_CHILD_AGES")


@dataclass(frozen=True)
class Request:
    city: str
    country: str
    checkin: str
    checkout: str
    rooms: tuple[Guests, ...]
    budget: str
    currency: str
    breakfast: bool = False
    free_cancel_until: datetime | None = None
    allowed_countries: tuple[str, ...] = ()
    payment_method: str | None = None

    def validate(self):
        if not isinstance(self.city, str) or not self.city.strip() or len(self.city) > 128:
            raise InputError("CITY_REQUIRED")
        if not isinstance(self.country, str) or not re.fullmatch("[A-Z]{2}", self.country):
            raise InputError("INVALID_COUNTRY")
        if not 1 <= (day(self.checkout) - day(self.checkin)).days <= 90:
            raise InputError("INVALID_STAY")
        if not isinstance(self.rooms, tuple) or not 1 <= len(self.rooms) <= 8:
            raise InputError("ROOMS_REQUIRED")
        for room in self.rooms:
            if not isinstance(room, Guests):
                raise InputError("INVALID_ROOM")
            room.validate()
        money(self.budget)
        if not isinstance(self.currency, str) or self.currency not in CURRENCIES:
            raise InputError("INVALID_CURRENCY")
        currency_money(self.budget, self.currency)
        if type(self.breakfast) is not bool:
            raise InputError("INVALID_BREAKFAST_FLAG")
        if self.free_cancel_until is not None:
            clock(self.free_cancel_until)
        if not isinstance(self.allowed_countries, tuple) or any(
            not isinstance(c, str) or not re.fullmatch("[A-Z]{2}", c)
            for c in self.allowed_countries
        ):
            raise InputError("INVALID_REGION_POLICY")
        if self.allowed_countries and self.country not in self.allowed_countries:
            raise InputError("REGION_NOT_ALLOWED")
        if self.payment_method is not None and self.payment_method not in ("STRIPE", "OTHER"):
            raise InputError("INVALID_PAYMENT_METHOD")


@dataclass(frozen=True)
class Offer:
    provider: str
    hotel_id: str
    room_id: str
    rate_id: str
    city: str
    country: str
    checkin: str
    checkout: str
    guests: tuple[Guests, ...]
    currency: str
    amount: str | None
    basis: str
    taxes_known: bool | None
    evidence: str
    observed_at: datetime
    source_ref: str
    breakfast: bool | None = None
    cancel_until: datetime | None = None
    capacity: int | None = None
    fee: str | None = None
    fee_method: str | None = None

    def validate(self):
        for s in (self.provider, self.hotel_id, self.room_id, self.rate_id, self.source_ref):
            if not isinstance(s, str) or not s.strip() or len(s) > 2048:
                raise InputError("OFFER_ID_OR_SOURCE_REQUIRED")
        if (
            not isinstance(self.currency, str)
            or self.currency not in CURRENCIES
            or not isinstance(self.evidence, str)
            or self.evidence not in ("ARCHIVED_RESPONSE", "SYNTHETIC", "PROVIDER_RESPONSE")
        ):
            raise InputError("OFFER_METADATA_INVALID")
        if self.basis not in ("PER_ROOM_PER_NIGHT", "FULL_STAY_ALL_ROOMS"):
            raise InputError("UNKNOWN_PRICE_BASIS")
        if self.taxes_known is not None and type(self.taxes_known) is not bool:
            raise InputError("TAX_FLAG_NOT_BOOLEAN")
        if self.breakfast is not None and type(self.breakfast) is not bool:
            raise InputError("BREAKFAST_FLAG_NOT_BOOLEAN")
        if self.amount is not None:
            currency_money(self.amount, self.currency)
        if self.fee is not None:
            currency_money(self.fee, self.currency)
            if not isinstance(self.fee_method, str) or not self.fee_method:
                raise InputError("FEE_METHOD_REQUIRED")
        if self.capacity is not None and (type(self.capacity) is not int or self.capacity < 1):
            raise InputError("INVALID_CAPACITY")
        if self.cancel_until is not None:
            clock(self.cancel_until)
        clock(self.observed_at)
        Request(
            self.city, self.country, self.checkin, self.checkout, self.guests, "0", self.currency
        ).validate()

    @property
    def identity(self):
        return (
            self.provider,
            self.hotel_id,
            self.room_id,
            self.rate_id,
            self.checkin,
            self.checkout,
            self.guests,
            self.currency,
            self.evidence,
        )


@dataclass(frozen=True)
class Decision:
    identity: tuple
    status: str
    reasons: tuple[str, ...]
    base_total: str | None
    payable_for_method: str | None
    fee_method: str | None
    currency: str
    evidence: str
    source_ref: str
    inventory: str = "NOT_VERIFIED"
    cancel_until: str | None = None


def evaluate(req: Request, offer: Offer, now: datetime, max_age: timedelta):
    offer.validate()
    reasons = []
    reject = []
    if (offer.city.casefold(), offer.country) != (req.city.casefold(), req.country):
        reject.append("LOCATION_MISMATCH")
    if (offer.checkin, offer.checkout, offer.guests) != (req.checkin, req.checkout, req.rooms):
        reject.append("REQUEST_CONTEXT_MISMATCH")
    if offer.observed_at > now or now - offer.observed_at > max_age:
        reasons.append("STALE_OR_FUTURE_SOURCE")
    if offer.currency != req.currency:
        reasons.append("FX_NOT_AVAILABLE")
    if offer.capacity is None:
        reasons.append("CAPACITY_UNKNOWN")
    elif any(g.adults + len(g.children) > offer.capacity for g in req.rooms):
        reject.append("CAPACITY_EXCEEDED")
    if req.breakfast:
        if offer.breakfast is None:
            reasons.append("BREAKFAST_UNKNOWN")
        elif not offer.breakfast:
            reject.append("BREAKFAST_NOT_INCLUDED")
    if req.free_cancel_until is not None:
        if offer.cancel_until is None:
            reasons.append("CANCELLATION_UNKNOWN")
        elif offer.cancel_until < req.free_cancel_until:
            reject.append("CANCELLATION_DEADLINE_TOO_EARLY")
    total = None
    payable = None
    if offer.amount is None:
        reasons.append("PRICE_UNKNOWN")
    else:
        total = money(offer.amount)
        if offer.basis == "PER_ROOM_PER_NIGHT":
            total *= len(req.rooms) * (day(req.checkout) - day(req.checkin)).days
        if offer.currency == req.currency and total > money(req.budget):
            reject.append("BASE_PRICE_OVER_BUDGET")
        if offer.taxes_known is not True:
            reasons.append("TAX_COVERAGE_UNVERIFIED")
        if offer.fee is not None:
            payable = total + money(offer.fee)
            if req.payment_method == offer.fee_method:
                if offer.currency == req.currency and payable > money(req.budget):
                    reject.append("SELECTED_PAYMENT_TOTAL_OVER_BUDGET")
            else:
                reasons.append("PAYMENT_METHOD_COST_UNVERIFIED")
    status = "REJECT" if reject else "REVIEW" if reasons else "CONSTRAINTS_MET_NOT_LIVE"
    return Decision(
        offer.identity,
        status,
        tuple(reject + reasons),
        str(total) if total is not None else None,
        str(payable) if payable is not None else None,
        offer.fee_method,
        offer.currency,
        offer.evidence,
        offer.source_ref,
        cancel_until=offer.cancel_until.isoformat() if offer.cancel_until is not None else None,
    )


class Provider(Protocol):
    def search(self, request: Request) -> list[Offer]: ...


@dataclass
class ListProvider:
    offers: list[Offer]

    def search(self, request):
        return list(self.offers)


@dataclass
class Engine:
    provider: Provider
    max_candidates: int = 10000
    result_limit: int = 5
    max_age: timedelta = timedelta(hours=24)
    total_limit_ms: float = 5000
    ranking_policy: str = "price"

    def search(self, request: Request, now: datetime):
        request.validate()
        clock(now)
        if type(self.max_candidates) is not int or not 1 <= self.max_candidates <= 100000:
            raise InputError("INVALID_CANDIDATE_LIMIT")
        if type(self.result_limit) is not int or not 1 <= self.result_limit <= 100:
            raise InputError("INVALID_RESULT_LIMIT")
        if not isinstance(self.max_age, timedelta) or self.max_age < timedelta(0):
            raise InputError("INVALID_MAX_AGE")
        if type(self.total_limit_ms) not in (int, float) or not 0 < self.total_limit_ms <= 60000:
            raise InputError("INVALID_TIME_LIMIT")
        if self.ranking_policy not in ("price", "cancellation"):
            raise InputError("INVALID_RANK_POLICY")
        start = time.perf_counter()
        decisions = []
        issues = []
        seen = set()
        scanned = 0
        try:
            offers = self.provider.search(request)
        except Exception as e:
            return {
                "status": "SOURCE_ERROR",
                "decisions": [],
                "issues": [type(e).__name__],
                "inventory": "NOT_VERIFIED",
            }
        if not isinstance(offers, list):
            return {
                "status": "SOURCE_ERROR",
                "decisions": [],
                "issues": ["INVALID_SOURCE_COLLECTION"],
                "inventory": "NOT_VERIFIED",
            }
        provider_elapsed = (time.perf_counter() - start) * 1000
        if provider_elapsed > self.total_limit_ms:
            return {
                "status": "TIME_LIMIT",
                "decisions": [],
                "issues": ["SOURCE_RETURNED_AFTER_DEADLINE"],
                "inventory": "NOT_VERIFIED",
            }
        truncated = len(offers) > self.max_candidates
        for offer in offers[: self.max_candidates]:
            if (time.perf_counter() - start) * 1000 > self.total_limit_ms:
                issues.append("TIME_LIMIT")
                truncated = True
                break
            scanned += 1
            try:
                if not isinstance(offer, Offer):
                    raise InputError("INVALID_OFFER_OBJECT")
                offer.validate()
                if offer.identity in seen:
                    raise InputError("DUPLICATE_EXACT_OFFER")
                seen.add(offer.identity)
                d = evaluate(request, offer, now, self.max_age)
                if (
                    self.ranking_policy == "cancellation"
                    and d.status == "CONSTRAINTS_MET_NOT_LIVE"
                    and d.cancel_until is None
                ):
                    d = replace(
                        d, status="REVIEW", reasons=d.reasons + ("RANK_CANCELLATION_UNKNOWN",)
                    )
                decisions.append(d)
            except (InputError, TypeError, AttributeError, InvalidOperation) as e:
                issues.append(str(e))
        # Different currency/evidence cohorts must not be presented as one ranking.
        eligible = [d for d in decisions if d.status == "CONSTRAINTS_MET_NOT_LIVE"]
        groups = {}
        for d in eligible:
            groups.setdefault((d.currency, d.evidence), []).append(d)

        def price_key(d):
            return money(
                d.payable_for_method
                if d.fee_method == request.payment_method and d.payable_for_method is not None
                else d.base_total
            )

        for cohort in groups.values():
            if self.ranking_policy == "cancellation":
                cohort.sort(
                    key=lambda d: (
                        -datetime.fromisoformat(d.cancel_until).timestamp(),
                        price_key(d),
                        d.identity,
                    )
                )
            else:
                cohort.sort(key=lambda d: (price_key(d), d.identity))
        return {
            "status": "PARTIAL" if truncated or issues else "OK",
            "decisions": decisions,
            "ranked_cohorts": {str(k): v[: self.result_limit] for k, v in groups.items()},
            "issues": issues,
            "truncated": truncated,
            "scanned": scanned,
            "source_rows": len(offers),
            "inventory": "NOT_VERIFIED",
            "provider_deadline_can_interrupt": False,
        }
