"""Indicative Frankfurter FX. Opt-in network, fail closed, not a checkout rate.

Frankfurter v2 ``GET /v2/rates?base=&quotes=`` returns a JSON array of
``date``, ``base``, ``quote``, ``rate``. The live pair observed while building
this module was shaped that way. ``rate`` is quote currency per 1 base unit.
Published reference rates are not a bank, card, or supplier checkout rate.

Rounding uses ``Decimal`` ``ROUND_HALF_UP`` at the ISO 4217 minor-unit exponent
listed in ``MINOR_UNITS``. Unlisted codes fail with ``CURRENCY_PRECISION_UNKNOWN``
rather than assuming 2 digits. A rate date is the provider calendar date. Freshness compares it with the
Europe/Berlin civil date, because that is the ECB publication zone, and also
rejects an observation older than 36 hours. This module does not retry uncertain calls.

``FRANKFURTER_LIVE_ENABLED`` stays False. Integrators must pass ``allow_remote=True``
for one anonymous GET. ``price_basis=UNKNOWN`` (the default) is never eligible
for a verified trip total. A transport fixture stays ineligible after it is
served again from the cache: only ``from_network`` observations qualify.
Conversion does not prove hotel tax or price basis.

Ordinary supplier amounts are strictly positive. A negative figure is rejected
with ``NONPOSITIVE`` and is not turned into a discount or a refund. Zero is
rejected the same way unless the caller passes ``fare_provenance="POLICY_FREE"``
for an officially free fare. ``amount is None`` stays unknown and is never
coerced to zero. ``verified_total`` defaults to a component subtotal:
``eligible_for_verified_total`` there does not mean a canonical TripPlan.
``trip_scope="whole_trip"`` requires hotel, transport, and transfer.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .errors import EnrichmentError
from .http_get import http_get, status_code, verified_tls_context

FRANKFURTER_LIVE_ENABLED = False
FRANKFURTER_RATES_URL = "https://api.frankfurter.dev/v2/rates"
SOURCE_NAME = "Frankfurter"
OFFICIAL_PUBLISHED_SOURCE = "Frankfurter v2 published reference rate"
ALLOWED_HOSTS = frozenset({"api.frankfurter.dev"})
MAX_BODY_BYTES = 65536
DEFAULT_TIMEOUT_SECONDS = 8.0
FRESHNESS_TTL = timedelta(hours=36)
MAX_RATE_AGE = timedelta(days=5)
BERLIN = ZoneInfo("Europe/Berlin")
_LIVE_TOKEN = object()
VERIFIED_PRICE_BASIS = frozenset({"NIGHTLY", "FULL_STAY", "PARTY_TOTAL", "LEG"})
KNOWN_TAX = frozenset({"INCLUDED", "EXCLUDED", "UNKNOWN"})
POLICY_FREE = "POLICY_FREE"
COMPONENT_SUBTOTAL = "components"
WHOLE_TRIP = "whole_trip"
WHOLE_TRIP_COMPONENTS = frozenset({"hotel", "transport", "transfer"})
_MONEY_INTEGRITY = frozenset({"NONPOSITIVE", "NONFINITE", "AMOUNT_TYPE", "FARE_PROVENANCE"})

# ISO 4217 minor-unit exponents. Not a live Frankfurter inventory.
MINOR_UNITS = {
    "AED": 2, "AFN": 2, "ALL": 2, "AMD": 2, "ANG": 2, "AOA": 2, "ARS": 2, "AUD": 2,
    "AWG": 2, "AZN": 2, "BAM": 2, "BBD": 2, "BDT": 2, "BGN": 2, "BHD": 3, "BIF": 0,
    "BMD": 2, "BND": 2, "BOB": 2, "BRL": 2, "BSD": 2, "BTN": 2, "BWP": 2, "BYN": 2,
    "BZD": 2, "CAD": 2, "CDF": 2, "CHF": 2, "CLF": 4, "CLP": 0, "CNY": 2, "COP": 2,
    "CRC": 2, "CUP": 2, "CVE": 2, "CZK": 2, "DJF": 0, "DKK": 2, "DOP": 2, "DZD": 2,
    "EGP": 2, "ERN": 2, "ETB": 2, "EUR": 2, "FJD": 2, "FKP": 2, "GBP": 2, "GEL": 2,
    "GHS": 2, "GIP": 2, "GMD": 2, "GNF": 0, "GTQ": 2, "GYD": 2, "HKD": 2, "HNL": 2,
    "HTG": 2, "HUF": 2, "IDR": 2, "ILS": 2, "INR": 2, "IQD": 3, "IRR": 2, "ISK": 0,
    "JMD": 2, "JOD": 3, "JPY": 0, "KES": 2, "KGS": 2, "KHR": 2, "KMF": 0, "KRW": 0,
    "KWD": 3, "KYD": 2, "KZT": 2, "LAK": 2, "LBP": 2, "LKR": 2, "LRD": 2, "LSL": 2,
    "LYD": 3, "MAD": 2, "MDL": 2, "MGA": 2, "MKD": 2, "MMK": 2, "MNT": 2, "MOP": 2,
    "MRU": 2, "MUR": 2, "MVR": 2, "MWK": 2, "MXN": 2, "MYR": 2, "MZN": 2, "NAD": 2,
    "NGN": 2, "NIO": 2, "NOK": 2, "NPR": 2, "NZD": 2, "OMR": 3, "PAB": 2, "PEN": 2,
    "PGK": 2, "PHP": 2, "PKR": 2, "PLN": 2, "PYG": 0, "QAR": 2, "RON": 2, "RSD": 2,
    "RUB": 2, "RWF": 0, "SAR": 2, "SBD": 2, "SCR": 2, "SDG": 2, "SEK": 2, "SGD": 2,
    "SHP": 2, "SLE": 2, "SOS": 2, "SRD": 2, "SSP": 2, "STN": 2, "SVC": 2, "SYP": 2,
    "SZL": 2, "THB": 2, "TJS": 2, "TMT": 2, "TND": 3, "TOP": 2, "TRY": 2, "TTD": 2,
    "TWD": 2, "TZS": 2, "UAH": 2, "UGX": 0, "USD": 2, "UYU": 2, "UZS": 2, "VES": 2,
    "VND": 0, "VUV": 0, "WST": 2, "XAF": 0, "XCD": 2, "XOF": 0, "XPF": 0, "YER": 2,
    "ZAR": 2, "ZMW": 2, "ZWG": 2,
}


@dataclass(frozen=True)
class RateObservation:
    base: str
    quote: str
    rate: Decimal
    as_of: date
    observed_at: datetime
    source_url: str
    source_name: str = SOURCE_NAME
    mode: str = "FIXTURE"
    indicative: bool = True
    checkout_rate: bool = False
    official_published_source: str = OFFICIAL_PUBLISHED_SOURCE
    _live_token: object = None

    def __post_init__(self) -> None:
        if self.checkout_rate or not self.indicative:
            raise EnrichmentError("NOT_A_CHECKOUT_RATE")
        if self.mode not in {"LIVE", "CACHED", "FIXTURE"}:
            raise EnrichmentError("FX_MODE")
        if self._live_token is not None and self._live_token is not _LIVE_TOKEN:
            raise EnrichmentError("LIVE_NOT_ISSUED")
        if self.mode == "FIXTURE" and self._live_token is not None:
            raise EnrichmentError("LIVE_NOT_ISSUED")
        if self.mode == "LIVE" and self._live_token is not _LIVE_TOKEN:
            raise EnrichmentError("LIVE_NOT_ISSUED")
        _validate_pair(self.base, self.quote)
        if not isinstance(self.rate, Decimal) or isinstance(self.rate, bool):
            raise EnrichmentError("RATE_TYPE")
        if not self.rate.is_finite():
            raise EnrichmentError("NONFINITE")
        if self.rate <= 0:
            raise EnrichmentError("NONPOSITIVE")
        if not isinstance(self.as_of, date) or isinstance(self.as_of, datetime):
            raise EnrichmentError("RATE_DATE")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise EnrichmentError("NAIVE_TIMESTAMP")
        if not self.source_url.startswith("https://api.frankfurter.dev/"):
            raise EnrichmentError("SOURCE_URL")

    @property
    def from_network(self) -> bool:
        """True only for a rate this process fetched, including its cache copy."""
        return self._live_token is _LIVE_TOKEN


@dataclass(frozen=True)
class MoneyConversion:
    amount: Decimal
    currency: str
    rate_observation: RateObservation
    price_basis: str
    tax_status: str
    eligible_for_verified_total: bool
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.amount, bool) or not isinstance(self.amount, Decimal):
            raise EnrichmentError("AMOUNT_TYPE")
        if not self.amount.is_finite():
            raise EnrichmentError("NONFINITE")
        if self.amount < 0:
            raise EnrichmentError("NONPOSITIVE")


class RateCache:
    """In-process freshness cache. Keys are pair plus the provider rate date."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, date], RateObservation] = {}

    def put(self, observation: RateObservation) -> None:
        self._items[(observation.base, observation.quote, observation.as_of)] = observation

    def get(self, base: str, quote: str, as_of: date, now: datetime) -> RateObservation | None:
        found = self._items.get((base, quote, as_of))
        if found is None:
            return None
        _require_fresh(found, now)
        return _as_cached(found)


def currency_code(value: str) -> str:
    if not isinstance(value, str) or value not in MINOR_UNITS:
        raise EnrichmentError("CURRENCY_CODE")
    return value


def _validate_pair(base: str, quote: str) -> None:
    currency_code(base)
    currency_code(quote)
    if base == quote:
        raise EnrichmentError("SAME_CURRENCY")


def minor_units(currency: str) -> int:
    code = currency_code(currency)
    try:
        return MINOR_UNITS[code]
    except KeyError:
        raise EnrichmentError("CURRENCY_PRECISION_UNKNOWN") from None


def quantize_money(amount: Decimal, currency: str) -> Decimal:
    if isinstance(amount, bool) or not isinstance(amount, Decimal):
        raise EnrichmentError("AMOUNT_TYPE")
    if not amount.is_finite():
        raise EnrichmentError("NONFINITE")
    if amount < 0:
        raise EnrichmentError("NONPOSITIVE")
    exponent = minor_units(currency)
    quantum = Decimal("1").scaleb(-exponent)
    return amount.quantize(quantum, rounding=ROUND_HALF_UP)


def fare_provenance_code(value) -> str | None:
    """Trusted zero-fare marker. Only ``POLICY_FREE`` is accepted."""
    if value is None:
        return None
    if value == POLICY_FREE:
        return POLICY_FREE
    raise EnrichmentError("FARE_PROVENANCE")


def parse_money_amount(amount, *, fare_provenance: str | None = None) -> Decimal:
    """Accept a positive supplier amount, or zero only for ``POLICY_FREE``.

    Floats are refused so a binary fraction cannot become a price. ``None`` is
    not a zero: callers keep an unknown amount unknown. A negative number is an
    ordinary price error, not a discount or a refund.
    """
    provenance = fare_provenance_code(fare_provenance)
    if isinstance(amount, str):
        try:
            amount = Decimal(amount)
        except InvalidOperation:
            raise EnrichmentError("AMOUNT_TYPE") from None
    if isinstance(amount, bool) or not isinstance(amount, Decimal):
        raise EnrichmentError("AMOUNT_TYPE")
    if not amount.is_finite():
        raise EnrichmentError("NONFINITE")
    if amount < 0:
        raise EnrichmentError("NONPOSITIVE")
    if amount == 0 and provenance != POLICY_FREE:
        raise EnrichmentError("NONPOSITIVE")
    return amount


def _aware_utc(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise EnrichmentError("NAIVE_TIMESTAMP")
    return current.astimezone(timezone.utc)


def _require_fresh(observation: RateObservation, now: datetime) -> None:
    observed = _aware_utc(observation.observed_at)
    current = _aware_utc(now)
    age = current - observed
    if age < timedelta(0) or age > FRESHNESS_TTL:
        raise EnrichmentError("STALE")
    berlin_today = current.astimezone(BERLIN).date()
    # Frankfurter can publish a rate date one Berlin day ahead of the fetch.
    if observation.as_of > berlin_today + timedelta(days=1):
        raise EnrichmentError("AS_OF_IN_FUTURE")
    if berlin_today - observation.as_of > MAX_RATE_AGE:
        raise EnrichmentError("STALE")


def _as_cached(observation: RateObservation) -> RateObservation:
    return RateObservation(
        base=observation.base,
        quote=observation.quote,
        rate=observation.rate,
        as_of=observation.as_of,
        observed_at=observation.observed_at,
        source_url=observation.source_url,
        source_name=observation.source_name,
        mode="CACHED",
        official_published_source=observation.official_published_source,
        _live_token=observation._live_token if observation._live_token is _LIVE_TOKEN else None,
    )


def build_rate_url(base: str, quote: str) -> str:
    _validate_pair(base, quote)
    query = urllib.parse.urlencode({"base": base, "quotes": quote})
    return FRANKFURTER_RATES_URL + "?" + query


def _strict_json(raw: bytes) -> object:
    if len(raw) > MAX_BODY_BYTES:
        raise EnrichmentError("OVERSIZE")

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise EnrichmentError("MALFORMED")
            out[key] = value
        return out

    def reject_constant(_value):
        raise EnrichmentError("NONFINITE")

    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=reject_constant,
        )
    except EnrichmentError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise EnrichmentError("MALFORMED") from None


def parse_frankfurter_rates(
    raw: bytes,
    *,
    requested_base: str,
    requested_quote: str,
    source_url: str,
    observed_at: datetime,
    mode: str = "FIXTURE",
    live_token: object = None,
) -> RateObservation:
    _validate_pair(requested_base, requested_quote)
    payload = _strict_json(raw)
    if isinstance(payload, dict):
        raise EnrichmentError("MALFORMED")
    if not isinstance(payload, list) or not payload:
        raise EnrichmentError("MISSING_PAIR")
    match = None
    for item in payload:
        if not isinstance(item, dict):
            raise EnrichmentError("MALFORMED")
        if item.get("base") == requested_base and item.get("quote") == requested_quote:
            if match is not None:
                raise EnrichmentError("MALFORMED")
            match = item
    if match is None:
        raise EnrichmentError("MISSING_PAIR")
    for field in ("date", "base", "quote", "rate"):
        if field not in match:
            raise EnrichmentError("MALFORMED")
    rate = match["rate"]
    if isinstance(rate, str):
        try:
            rate = Decimal(rate)
        except InvalidOperation:
            raise EnrichmentError("RATE_TYPE") from None
    if isinstance(rate, bool) or not isinstance(rate, Decimal):
        raise EnrichmentError("RATE_TYPE")
    try:
        as_of = date.fromisoformat(match["date"]) if isinstance(match["date"], str) else None
    except ValueError:
        as_of = None
    if as_of is None or match["date"] != as_of.isoformat():
        raise EnrichmentError("RATE_DATE")
    return RateObservation(
        base=requested_base,
        quote=requested_quote,
        rate=rate,
        as_of=as_of,
        observed_at=_aware_utc(observed_at),
        source_url=source_url,
        mode=mode,
        _live_token=live_token if mode == "LIVE" else None,
    )


def fetch_frankfurter_rate(
    base: str,
    quote: str,
    *,
    transport=None,
    allow_remote: bool | None = None,
    cache: RateCache | None = None,
    now: datetime | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> RateObservation:
    """Fetch one pair. Default is no network. ``transport(url)`` returns ``BoundedResponse``."""
    url = build_rate_url(base, quote)
    current = _aware_utc(now)
    if cache is not None:
        for observation in list(cache._items.values()):
            if observation.base == base and observation.quote == quote:
                try:
                    _require_fresh(observation, current)
                except EnrichmentError:
                    continue
                return _as_cached(observation)
    enabled = FRANKFURTER_LIVE_ENABLED if allow_remote is None else allow_remote
    if transport is None and not enabled:
        raise EnrichmentError("LIVE_DISABLED")
    network = transport is None
    if network:
        host = urllib.parse.urlparse(url).hostname
        if host not in ALLOWED_HOSTS:
            raise EnrichmentError("HOST_REFUSED")

        def transport(target: str):
            return http_get(
                target,
                timeout=timeout,
                max_bytes=MAX_BODY_BYTES,
                ssl_context=verified_tls_context(),
            )

    try:
        response = transport(url)
    except EnrichmentError:
        raise
    except (TimeoutError, socket_timeout()):
        raise EnrichmentError("TIMEOUT") from None
    if getattr(response, "status", 200) != 200:
        raise EnrichmentError(status_code(response.status))
    body = response.body
    if len(body) > MAX_BODY_BYTES:
        raise EnrichmentError("OVERSIZE")
    source_url = getattr(response, "url", None) or url
    observation = parse_frankfurter_rates(
        body,
        requested_base=base,
        requested_quote=quote,
        source_url=source_url,
        observed_at=current,
        mode="LIVE" if network else "FIXTURE",
        live_token=_LIVE_TOKEN if network else None,
    )
    if cache is not None:
        cache.put(observation)
    return observation


def socket_timeout():
    import socket

    return socket.timeout


def convert_money(
    amount,
    rate_observation: RateObservation,
    *,
    price_basis: str = "UNKNOWN",
    tax_status: str = "UNKNOWN",
    now: datetime | None = None,
    require_fresh: bool = True,
    fare_provenance: str | None = None,
) -> MoneyConversion:
    """Convert ``amount`` of ``rate_observation.base`` into the quote currency.

    Float amounts are refused. ``price_basis='UNKNOWN'`` stays ineligible for a
    verified total. Tax status ``UNKNOWN`` also stays ineligible. ``EXCLUDED``
    tax converts the known figure but is not an all-in verified total.

    Zero raises ``NONPOSITIVE`` unless ``fare_provenance="POLICY_FREE"``. A
    policy-free zero converts to zero in the quote currency without depending
    on the numeric rate: the observation is still required by this signature
    so the quote currency and freshness checks stay explicit. Callers that do
    not have a rate should use ``verified_total``, which accepts that zero
    across currencies on its own.
    """
    amount = parse_money_amount(amount, fare_provenance=fare_provenance)
    if not isinstance(rate_observation, RateObservation):
        raise EnrichmentError("RATE_TYPE")
    if require_fresh:
        _require_fresh(rate_observation, _aware_utc(now))
    if not isinstance(price_basis, str) or price_basis not in VERIFIED_PRICE_BASIS | {"UNKNOWN", "ESTIMATE"}:
        raise EnrichmentError("PRICE_BASIS")
    if not isinstance(tax_status, str) or tax_status not in KNOWN_TAX:
        raise EnrichmentError("TAX_STATUS")
    if amount == 0:
        converted = quantize_money(Decimal(0), rate_observation.quote)
    else:
        converted = quantize_money(amount * rate_observation.rate, rate_observation.quote)
        if converted <= 0:
            raise EnrichmentError("NONPOSITIVE")
    warnings = ["INDICATIVE_NOT_CHECKOUT"]
    if amount == 0:
        warnings.append("POLICY_FREE")
    eligible = (
        price_basis in VERIFIED_PRICE_BASIS
        and tax_status == "INCLUDED"
        and rate_observation.from_network
        and rate_observation.mode in {"LIVE", "CACHED"}
    )
    if not rate_observation.from_network:
        warnings.append("FX_NOT_LIVE")
    if price_basis == "UNKNOWN":
        warnings.append("PRICE_BASIS_UNKNOWN")
    elif price_basis == "ESTIMATE":
        warnings.append("ESTIMATE_EXCLUDED")
    if tax_status == "UNKNOWN":
        warnings.append("TAX_UNKNOWN")
    elif tax_status == "EXCLUDED":
        warnings.append("TAX_EXCLUDED")
    return MoneyConversion(
        amount=converted,
        currency=rate_observation.quote,
        rate_observation=rate_observation,
        price_basis=price_basis,
        tax_status=tax_status,
        eligible_for_verified_total=eligible,
        warnings=tuple(warnings),
    )


def contribute_to_verified_total(running: Decimal | None, conversion: MoneyConversion, total_currency: str) -> Decimal:
    """Add one eligible same-currency conversion. Refuses UNKNOWN basis and mixed currency."""
    if not isinstance(conversion, MoneyConversion) or not conversion.eligible_for_verified_total:
        raise EnrichmentError("EXCLUDED_FROM_VERIFIED_TOTAL")
    if conversion.amount < 0:
        raise EnrichmentError("NONPOSITIVE")
    code = currency_code(total_currency)
    if conversion.currency != code or conversion.rate_observation.quote != code:
        raise EnrichmentError("MIXED_CURRENCY")
    if running is None:
        return conversion.amount
    if not isinstance(running, Decimal):
        raise EnrichmentError("AMOUNT_TYPE")
    return quantize_money(running + conversion.amount, code)


def map_money_line(component: dict, rate_observation: RateObservation | None = None, **convert_kwargs) -> dict:
    """Pure mapping for a TripPlan-shaped line. Does not mutate the input.

    ``amount is None`` stays null and is excluded. It is not coerced to zero.
    A same-currency line may omit the rate. A different currency without a dated
    observation fails with ``MISSING_PAIR``. Fixture rates are not verified totals.
    A negative amount raises ``NONPOSITIVE``. Zero raises the same code unless
    the line sets ``fare_provenance`` to ``POLICY_FREE``.
    """
    if not isinstance(component, dict):
        raise EnrichmentError("MALFORMED")
    amount = component.get("amount")
    currency = component.get("currency")
    price_basis = component.get("price_basis", "UNKNOWN")
    tax_status = component.get("tax_status", "UNKNOWN")
    source_mode = component.get("source_mode", "UNKNOWN")
    provenance = fare_provenance_code(component.get("fare_provenance"))
    if amount is None:
        return {
            "amount": None,
            "currency": currency_code(currency) if isinstance(currency, str) else None,
            "price_basis": price_basis,
            "eligible_for_verified_total": False,
            "warnings": ("AMOUNT_UNKNOWN",),
            "converted": None,
            "checkout_rate": False,
        }
    if rate_observation is None:
        code = currency_code(currency)
        parsed = parse_money_amount(amount, fare_provenance=provenance)
        quantized = quantize_money(parsed, code)
        if parsed != 0 and quantized <= 0:
            raise EnrichmentError("NONPOSITIVE")
        eligible = price_basis in VERIFIED_PRICE_BASIS and tax_status == "INCLUDED" and source_mode == "LIVE"
        warnings = ["INDICATIVE_NOT_CHECKOUT"]
        if parsed == 0:
            warnings.append("POLICY_FREE")
        if price_basis == "UNKNOWN":
            warnings.append("PRICE_BASIS_UNKNOWN")
        if source_mode != "LIVE":
            warnings.append("NON_LIVE_COMPONENT")
        return {
            "amount": format(quantized, "f"),
            "currency": code,
            "price_basis": price_basis,
            "tax_status": tax_status,
            "eligible_for_verified_total": eligible,
            "warnings": tuple(warnings),
            "source_url": None,
            "as_of": None,
            "fx_mode": None,
            "indicative": True,
            "checkout_rate": False,
            "converted": False,
        }
    if currency_code(currency) != rate_observation.base:
        raise EnrichmentError("MIXED_CURRENCY")
    conversion = convert_money(
        amount,
        rate_observation,
        price_basis=price_basis,
        tax_status=tax_status,
        fare_provenance=provenance,
        **convert_kwargs,
    )
    eligible = conversion.eligible_for_verified_total and source_mode == "LIVE"
    warnings = list(conversion.warnings)
    if source_mode != "LIVE":
        warnings.append("NON_LIVE_COMPONENT")
    return {
        "amount": format(conversion.amount, "f"),
        "currency": conversion.currency,
        "price_basis": conversion.price_basis,
        "tax_status": conversion.tax_status,
        "eligible_for_verified_total": eligible,
        "warnings": tuple(dict.fromkeys(warnings)),
        "source_url": conversion.rate_observation.source_url,
        "as_of": conversion.rate_observation.as_of.isoformat(),
        "fx_mode": conversion.rate_observation.mode,
        "indicative": True,
        "checkout_rate": False,
    }


def verified_total(
    components,
    target_currency: str,
    rates=(),
    *,
    now: datetime | None = None,
    trip_scope: str = COMPONENT_SUBTOTAL,
    required_components=None,
) -> dict:
    """Sum known-basis lines. Unknown basis or a missing dated rate blanks the total.

    Same-currency lines are added without inventing an FX rate. A different
    currency needs exactly one fresh LIVE or CACHED observation for that pair.
    Fixture rates do not qualify. Checkout is always false.

    The default ``trip_scope="components"`` is a component subtotal. Its
    ``eligible_for_verified_total`` flag does not claim a canonical TripPlan,
    and ``covers_whole_trip`` stays false. ``trip_scope="whole_trip"`` (or an
    explicit ``required_components`` set) requires every named component,
    including hotel, transport, and transfer for a whole trip. One hotel is
    not that claim. A policy-free zero is zero in the target currency with no
    provider rate. A missing amount stays missing and is not stored as zero.
    Negative amounts raise ``NONPOSITIVE`` instead of reducing the sum.
    """
    target = currency_code(target_currency)
    if isinstance(components, (str, bytes)) or isinstance(rates, (str, bytes)):
        raise EnrichmentError("MALFORMED")
    if not isinstance(trip_scope, str) or trip_scope not in {COMPONENT_SUBTOTAL, WHOLE_TRIP}:
        raise EnrichmentError("TRIP_SCOPE")
    if required_components is None:
        needed = set(WHOLE_TRIP_COMPONENTS) if trip_scope == WHOLE_TRIP else set()
    else:
        if isinstance(required_components, (str, bytes)):
            raise EnrichmentError("MALFORMED")
        try:
            needed = set(required_components)
        except TypeError:
            raise EnrichmentError("MALFORMED") from None
        if not all(isinstance(item, str) and item for item in needed):
            raise EnrichmentError("MALFORMED")
        if trip_scope == WHOLE_TRIP:
            needed |= set(WHOLE_TRIP_COMPONENTS)
    warnings: list[str] = []
    excluded: list[str] = []
    parts: list[Decimal] = []
    included: set[str] = set()
    blocked = False
    rate_list = list(rates)

    def decision(amount, eligible: bool, missing: tuple[str, ...]) -> dict:
        covers = bool(
            eligible
            and trip_scope == WHOLE_TRIP
            and not missing
            and WHOLE_TRIP_COMPONENTS <= included
        )
        return {
            "amount": amount,
            "currency": target,
            "eligible_for_verified_total": eligible,
            "covers_whole_trip": covers,
            "trip_scope": trip_scope,
            "missing_components": missing,
            "checkout_verified": False,
            "warnings": tuple(dict.fromkeys(warnings)),
            "excluded": tuple(excluded),
        }

    for component in components:
        if not isinstance(component, dict):
            raise EnrichmentError("MALFORMED")
        raw_name = component.get("component")
        name = raw_name if isinstance(raw_name, str) else ""
        label = str(name or component.get("currency") or "line")
        required = component.get("required", True)
        if not isinstance(required, bool):
            raise EnrichmentError("MALFORMED")
        basis = component.get("price_basis", "UNKNOWN")
        mode = component.get("source_mode", "UNKNOWN")
        amount = component.get("amount")
        currency = component.get("currency")
        provenance = fare_provenance_code(component.get("fare_provenance"))
        parsed = None if amount is None else parse_money_amount(amount, fare_provenance=provenance)
        if amount is None or basis not in VERIFIED_PRICE_BASIS or mode != "LIVE" or component.get("tax_status", "UNKNOWN") != "INCLUDED":
            excluded.append(label)
            if basis == "UNKNOWN" or basis not in VERIFIED_PRICE_BASIS:
                warnings.append("PRICE_BASIS_UNKNOWN")
            if mode != "LIVE":
                warnings.append("NON_LIVE_COMPONENT")
            if component.get("tax_status", "UNKNOWN") != "INCLUDED":
                warnings.append("TAX_UNKNOWN")
            if required:
                blocked = True
            continue
        code = currency_code(currency)
        if parsed == 0:
            # Zero is zero in every currency, so a dated provider rate is not required.
            parts.append(quantize_money(Decimal(0), target))
            included.add(name)
            warnings.append("POLICY_FREE")
            continue
        if code == target:
            quantized = quantize_money(parsed, target)
            if quantized <= 0:
                raise EnrichmentError("NONPOSITIVE")
            parts.append(quantized)
            included.add(name)
            continue
        matches = [
            rate for rate in rate_list
            if isinstance(rate, RateObservation)
            and rate.base == code
            and rate.quote == target
            and rate.from_network
            and rate.mode in {"LIVE", "CACHED"}
        ]
        if len(matches) != 1:
            warnings.append("MISSING_DATED_FX" if not matches else "AMBIGUOUS_FX")
            excluded.append(label)
            if required:
                blocked = True
            continue
        try:
            converted = convert_money(
                amount,
                matches[0],
                price_basis=basis,
                tax_status="INCLUDED",
                fare_provenance=provenance,
                now=now,
            )
        except EnrichmentError as exc:
            if exc.code in _MONEY_INTEGRITY:
                raise
            warnings.append(exc.code)
            excluded.append(label)
            if required:
                blocked = True
            continue
        if converted.amount < 0:
            raise EnrichmentError("NONPOSITIVE")
        parts.append(converted.amount)
        included.add(name)
    missing = tuple(sorted(needed - included))
    if missing:
        blocked = True
        warnings.append("INCOMPLETE_TRIP")
    if blocked or not parts:
        return decision(None, False, missing)
    total = quantize_money(sum(parts, Decimal(0)), target)
    if total < 0:
        raise EnrichmentError("NONPOSITIVE")
    return decision(format(total, "f"), True, missing)
