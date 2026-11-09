"""ETG/Ostrovok V3 read-only search adapter — sandbox fixture by default.

Official paths: https://docs.emergingtravel.com/docs/integration-guide/
Search by region B2B: /api/b2b/v3/search/serp/region/.
Partner API credentials are NOT bundled; booking/payment methods are prohibited.
Nothing in this module calls a provider unless search_sandbox is explicitly
invoked with a sandbox-only credential, approved region and authorization.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import base64, json, urllib.request, urllib.error, ssl, re, time, sqlite3, threading, hashlib, secrets

SANDBOX_HOST = "https://api-sandbox.ratehawk.com"
PATH = "/api/b2b/v3/search/serp/region/"
MAX_RESPONSE_BYTES = 2_000_000


class SupplierContractError(ValueError):
    pass


class SupplierUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class SearchCriteria:
    checkin: str
    checkout: str
    region_id: int
    residency: str
    currency: str
    guests: tuple
    hotels_limit: int = 3


def make_request(criteria, today=None):
    if not isinstance(criteria, SearchCriteria):
        raise SupplierContractError("TYPED_CRITERIA_REQUIRED")
    today = today or datetime.now(timezone.utc).date()
    try:
        ci = date.fromisoformat(criteria.checkin)
        co = date.fromisoformat(criteria.checkout)
    except (ValueError, TypeError):
        raise SupplierContractError("INVALID_DATE")
    if not (today <= ci <= today + timedelta(days=730) and ci < co <= ci + timedelta(days=30)):
        raise SupplierContractError("DATES_OUTSIDE_PROVIDER_LIMITS")
    if type(criteria.region_id) is not int or criteria.region_id <= 0:
        raise SupplierContractError("REGION_ID_MUST_BE_VERIFIED")
    if not isinstance(criteria.residency, str) or not re.fullmatch("[a-z]{2}", criteria.residency):
        raise SupplierContractError("GUEST_RESIDENCY_REQUIRED")
    if not isinstance(criteria.currency, str) or not re.fullmatch("[A-Z]{3}", criteria.currency):
        raise SupplierContractError("CURRENCY_ISO_REQUIRED")
    if type(criteria.hotels_limit) is not int or not 1 <= criteria.hotels_limit <= 20:
        raise SupplierContractError("HOTEL_LIMIT_BOUND")
    if not isinstance(criteria.guests, tuple) or not criteria.guests or len(criteria.guests) > 9:
        raise SupplierContractError("GUEST_ROOMS_REQUIRED")
    guests = []
    for room in criteria.guests:
        if not isinstance(room, dict) or set(room) != {"adults", "children"}:
            raise SupplierContractError("ROOM_SCHEMA")
        adults = room["adults"]
        children = room["children"]
        if type(adults) is not int or not 1 <= adults <= 6:
            raise SupplierContractError("ADULTS_INVALID")
        if (
            not isinstance(children, list)
            or len(children) > 4
            or any(type(c) is not int or c < 0 or c > 17 for c in children)
        ):
            raise SupplierContractError("CHILD_AGES_MUST_BE_EXPLICIT")
        guests.append({"adults": adults, "children": list(children)})
    return {
        "checkin": criteria.checkin,
        "checkout": criteria.checkout,
        "region_id": criteria.region_id,
        "residency": criteria.residency,
        "language": "ru",
        "currency": criteria.currency,
        "guests": guests,
        "hotels_limit": criteria.hotels_limit,
    }


def money(value):
    if not isinstance(value, str) or len(value) > 25:
        return None
    try:
        d = Decimal(value)
        if not d.is_finite() or d < 0:
            return None
        return str(d)
    except InvalidOperation:
        return None


def normalize_search_response(payload, criteria, *, source="ETG_SANDBOX_FIXTURE", observed_at=None):
    """No availability/price guarantees. No invented hotel names, totals, or identities."""
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ok"
        or not isinstance(payload.get("data"), dict)
    ):
        raise SupplierContractError("SUPPLIER_RESPONSE_UNVERIFIED")
    hotels = payload["data"].get("hotels")
    if not isinstance(hotels, list):
        raise SupplierContractError("SUPPLIER_HOTELS_MISSING")
    if len(hotels) > 100:
        raise SupplierContractError("SUPPLIER_TOO_MANY_HOTELS")
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    output = []
    for hotel in hotels:
        if not isinstance(hotel, dict):
            raise SupplierContractError("HOTEL_SCHEMA")
        hid = hotel.get("hid")
        old_id = hotel.get("id")
        if (type(hid) is not int or hid <= 0) and (not isinstance(old_id, str) or not old_id):
            raise SupplierContractError("HOTEL_ID_MISSING")
        rate_items = hotel.get("rates", [])
        sorted_rates = sorted(
            rate_items,
            key=lambda r: (
                not bool(
                    r.get("meal_data", {}).get("has_breakfast")
                    if isinstance(r, dict) and isinstance(r.get("meal_data"), dict)
                    else False
                )
            ),
        )
        for rate in sorted_rates[:10]:
            if not isinstance(rate, dict):
                raise SupplierContractError("RATE_SCHEMA")
            token = rate.get("search_hash")
            if not isinstance(token, str) or not token or len(token) > 512:
                raise SupplierContractError("RATE_HASH_REQUIRED")
            payment_options = rate.get("payment_options", {})
            if not isinstance(payment_options, dict):
                raise SupplierContractError("PAYMENT_OPTIONS_SCHEMA")
            payment_types = payment_options.get("payment_types", [])
            if not isinstance(payment_types, list):
                raise SupplierContractError("PAYMENT_TYPES_SCHEMA")
            candidate = None
            for payment in payment_types:
                if not isinstance(payment, dict):
                    continue
                cur = payment.get("show_currency_code")
                value = money(payment.get("show_amount"))
                if cur == criteria.currency and value is not None:
                    candidate = {
                        "show_amount": value,
                        "currency": cur,
                        "payment_type": payment.get("type"),
                        "tax_evidence": payment.get("tax_data"),
                        "fee_evidence": payment.get("fees"),
                    }
                    break
            details = rate.get("meal_data", {})
            breakfast = details.get("has_breakfast") if isinstance(details, dict) else None
            if type(breakfast) is not bool:
                breakfast = None
            output.append(
                {
                    "hotel_id": hid if type(hid) is int and hid > 0 else old_id,
                    "hotel_name": None,  # static content API is required for names
                    "rate_ref_sha256": hashlib.sha256(token.encode("utf8")).hexdigest(),
                    "rate_search_hash_present": True,
                    "room_name": None,
                    "quote_checkin": criteria.checkin,
                    "quote_checkout": criteria.checkout,
                    "quote_guests": make_request(criteria)["guests"],
                    "base_or_show_amount": candidate,
                    "total_price_verified": False,
                    "taxes_fully_verified": False,
                    "breakfast_included": breakfast,
                    "availability_now_verified": False,
                    "bookable_now_verified": False,
                    "supplier_status": "REVIEW_BEFORE_PREBOOK",
                    "source": source,
                    "observed_at": observed_at,
                    "provider_direct_booking_enabled": False,
                    "unknowns": [
                        "HOTEL_STATIC_CONTENT",
                        "FINAL_PAYABLE_TOTAL",
                        "ALL_TAXES_FEES",
                        "ROOM_ALLOTMENT",
                        "RECHECK_HOTELPAGE_AND_PREBOOK",
                    ],
                }
            )
    return {
        "source": source,
        "supplier": "ETG_Ostrovok_V3",
        "live_inventory_confirmed": False,
        "booking_enabled": False,
        "hotel_count": len(hotels),
        "rates": output,
        "search_criteria": make_request(criteria),
        "provider_response_status": "ok",
        "note": "SERP alone is NOT a final payable amount or a successful booking.",
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SupplierUnavailable("REDIRECT_DENIED")


class SandboxSearchClient:
    def __init__(self, *, ledger, opener=None):
        self.ledger = Path(ledger)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.opener = opener or urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )
        self.db = sqlite3.connect(self.ledger, check_same_thread=False, isolation_level=None)
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,status TEXT,request_sha TEXT)"
        )

    def close(self):
        self.db.close()

    def search_sandbox(self, criteria, *, sandbox_key_id, sandbox_secret, owner_authorized=False):
        if owner_authorized is not True:
            raise SupplierUnavailable("SUPPLIER_AUTHORIZATION_REQUIRED")
        if (
            not isinstance(sandbox_key_id, str)
            or not sandbox_key_id
            or not isinstance(sandbox_secret, str)
            or not sandbox_secret
        ):
            raise SupplierUnavailable("SANDBOX_CREDENTIAL_REQUIRED")
        data = json.dumps(make_request(criteria), ensure_ascii=False, separators=(",", ":")).encode(
            "utf8"
        )
        if len(data) > 8192:
            raise SupplierContractError("INPUT_TOO_LARGE")
        digest = hashlib.sha256(data).hexdigest()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] >= 1:
                    raise SupplierUnavailable("SANDBOX_ONE_REQUEST_LIMIT")
                ident = secrets.token_hex(12)
                self.db.execute(
                    "INSERT INTO attempts VALUES(?,?,?)", (ident, "UNKNOWN_OUTCOME", digest)
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        token = base64.b64encode((sandbox_key_id + ":" + sandbox_secret).encode("utf8")).decode(
            "ascii"
        )
        req = urllib.request.Request(
            SANDBOX_HOST + PATH,
            data=data,
            method="POST",
            headers={
                "Authorization": "Basic " + token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with self.opener.open(req, timeout=30) as response:
                if response.status != 200:
                    raise SupplierUnavailable("SUPPLIER_UNVERIFIED_HTTP")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise SupplierUnavailable("RESPONSE_TOO_LARGE")
            if sandbox_secret.encode("utf8") in raw:
                raise SupplierUnavailable("SECRET_ECHO")
            normalized = normalize_search_response(
                json.loads(raw), criteria, source="ETG_SANDBOX_RESPONSE_NOT_BOOKABLE"
            )
            with self.lock:
                self.db.execute(
                    "UPDATE attempts SET status='RESPONSE_RECEIVED' WHERE id=?", (ident,)
                )
            return normalized
        except BaseException:
            # Unknown outcome, never reissue automatically
            raise SupplierUnavailable("SANDBOX_OUTCOME_UNKNOWN_NO_RETRY") from None


def public_supplier_status():
    return {
        "provider": "ETG_Ostrovok_V3",
        "contract_implemented": True,
        "sandbox_credentials_configured": False,
        "live_search_enabled": False,
        "booking_enabled": False,
        "mode": "CONTRACT_AND_FIXTURE_VERIFIED",
        "price_availability_claim": "UNKNOWN_NO_PROVIDER_KEY",
        "requires": [
            "PARTNER_SANDBOX_KEY",
            "VERIFIED_REGION_ID",
            "GUEST_RESIDENCY",
            "REAL_SUPPLIER_APPROVAL",
            "SANDBOX_TEST_THEN_CERTIFICATION",
        ],
        "official_docs": "https://docs.emergingtravel.com/docs/integration-guide/",
    }
