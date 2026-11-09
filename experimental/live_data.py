from payload_retention import cleanup_public, receipt_metadata
import deadline_http
import request_deadline as deadline

"""Public read-only data adapters. No booking/account/payment endpoints or credentials."""
from pathlib import Path
import json, time, hashlib, os, urllib.request, urllib.parse, urllib.error, threading, math, re
from datetime import datetime, date, timezone, timedelta
from email.utils import parsedate_to_datetime
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, asdict
from contextlib import contextmanager
from supplier_dispatch import SupplierDispatchJournal
from trusted_tls import verified_context
from provider_transport import NoRedirect, strict_json, now, canonical


class DataError(ValueError):
    pass


ENDPOINTS = {
    "location": "search_location",
    "search": "search_hotels",
    "detail": "get_hotel_detail",
    "rates": "query_room_rates",
    "batch": "batch_query_room_rates",
    "availability": "check_room_availability",
    "version": "check_skill_update",
}


@dataclass
class HotelOffer:
    source: str
    observed_at: str
    hotel_id: str
    hotel_name: str
    room_id: str
    room_name: str
    rate_code: str
    check_in: str
    check_out: str
    occupancy: list
    room_count: int
    total: str | None
    currency: str
    price_basis: str = "FULL_STAY_ALL_ROOMS"
    taxes_known: bool | None = None
    cancellation: dict | None = None
    capacity: int | None = None
    breakfast: bool | None = None
    on_request: bool | None = None
    supplier_facts: dict | None = None
    availability_verified: bool = False
    availability_observed_at: str | None = None
    booking_confirmation_available: bool = False


def number(v):
    if isinstance(v, bool):
        raise DataError("INVALID_PRICE")
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        raise DataError("INVALID_PRICE")
    if not d.is_finite() or d < 0:
        raise DataError("INVALID_PRICE")
    return d


def validated_fx_rate(fx, currency, clock=None):
    # 'verified' is an assertion, not proof. Validate timestamps, identity,
    # positivity and finite Decimal rates before a cross-currency comparison.
    if not isinstance(fx, dict) or fx.get("verified") is not True:
        return None
    rates = fx.get("rates")
    if not isinstance(rates, dict) or currency not in rates:
        return None
    clock = clock or datetime.now(timezone.utc)
    if not isinstance(clock, datetime) or clock.tzinfo is None:
        return None
    try:
        stamp = fx.get("data_updated_at")
        if not isinstance(stamp, str):
            return None
        try:
            updated = datetime.fromisoformat(stamp)
        except ValueError:
            updated = parsedate_to_datetime(stamp)
        if updated.tzinfo is None:
            return None
        age = (clock - updated).total_seconds()
        if not 0 <= age <= 172800:
            return None

        def positive(v):
            if isinstance(v, bool):
                return None
            x = Decimal(str(v))
            if not x.is_finite() or x <= 0:
                return None
            return x

        base = positive(rates.get("CNY"))
        if base != Decimal("1"):
            return None
        return positive(rates[currency])
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def verify_offers(offers, request, fx=None, clock=None):
    clock = clock or datetime.now(timezone.utc)
    out = []
    seen = {}
    for item in offers:
        o = asdict(item) if isinstance(item, HotelOffer) else dict(item)
        reject = []
        unknown = []
        identity = (o["hotel_id"], o["room_id"], o["rate_code"])
        if identity in seen:
            if seen[identity].get("total") != o.get("total"):
                seen[identity]["status"] = "REJECT"
                seen[identity]["reasons"].append("DUPLICATE_CONFLICT")
            continue
        if (o["check_in"], o["check_out"], o["occupancy"], o["room_count"]) != (
            request["checkin"],
            request["checkout"],
            request["rooms"],
            len(request["rooms"]),
        ):
            reject.append("REQUEST_CONTEXT_MISMATCH")
        if o.get("price_basis") != "FULL_STAY_ALL_ROOMS":
            unknown.append("PRICE_BASIS_UNKNOWN")
        try:
            age = clock - datetime.fromisoformat(o["observed_at"])
            if age < timedelta(0) or age > timedelta(minutes=5):
                unknown.append("STALE_SOURCE")
        except Exception:
            reject.append("INVALID_TIMESTAMP")
        total = None
        try:
            total = number(o["total"]) if o["total"] is not None else None
        except DataError:
            reject.append("INVALID_PRICE")
        if total is None:
            unknown.append("PRICE_UNKNOWN")
        budget = number(request["budget"])
        budget_currency = request["currency"]
        converted = None
        if total is not None:
            if o["currency"] == budget_currency:
                converted = total
            elif (
                o["currency"] == "CNY"
                and (rate_fx := validated_fx_rate(fx, budget_currency, clock)) is not None
            ):
                converted = total * rate_fx
                o["approximate_budget_total"] = str(converted)
                o["fx"] = fx
            else:
                unknown.append("FX_NOT_AVAILABLE")
            if converted is not None and converted > budget:
                reject.append("BASE_PRICE_OVER_BUDGET")
        for party in request["rooms"]:
            if o.get("capacity") is None:
                unknown.append("CAPACITY_UNKNOWN")
            elif party["adults"] + len(party.get("children", [])) > o["capacity"]:
                reject.append("CAPACITY_EXCEEDED")
        if request.get("breakfast"):
            if o.get("breakfast") is False:
                reject.append("BREAKFAST_NOT_INCLUDED")
            elif o.get("breakfast") is not True:
                unknown.append("BREAKFAST_UNKNOWN")
        if o.get("taxes_known") is not True:
            unknown.append("TAX_COVERAGE_UNVERIFIED")
        if request.get("free_cancel_until"):
            policy = o.get("cancellation") or {}
            deadline = policy.get("free_cancel_deadline")
            if policy.get("effective_non_refundable") is True:
                reject.append("NON_REFUNDABLE")
            elif not deadline:
                unknown.append("CANCELLATION_UNKNOWN")
            else:
                try:
                    if datetime.fromisoformat(deadline) < datetime.fromisoformat(
                        request["free_cancel_until"]
                    ):
                        reject.append("CANCELLATION_DEADLINE_TOO_EARLY")
                except ValueError:
                    unknown.append("CANCELLATION_UNKNOWN")
        if o.get("on_request") is True:
            unknown.append("SUPPLIER_CONFIRMATION_REQUIRED")
        if o.get("availability_verified") is not True:
            unknown.append("AVAILABILITY_NOT_CHECKED")
        method = request.get("payment_method")
        fee = (o.get("supplier_facts") or {}).get("payment_fees", {}).get(method)
        if fee and fee.get("currency") == budget_currency:
            payable = number(fee["payable_amount"])
            o["payable_for_method"] = str(payable)
            if payable > budget:
                reject.append("SELECTED_PAYMENT_TOTAL_OVER_BUDGET")
        # Product count is not evidence of number of rooms remaining.
        if o["room_count"] > 1:
            unknown.append("ROOM_COUNT_AVAILABILITY_UNKNOWN")
        o["reasons"] = list(dict.fromkeys(reject + unknown))
        o["status"] = "REJECT" if reject else "REVIEW" if unknown else "CONSTRAINTS_MET_SNAPSHOT"
        out.append(o)
        seen[identity] = o
    return sorted(
        out,
        key=lambda o: (
            {"CONSTRAINTS_MET_SNAPSHOT": 0, "REVIEW": 1, "REJECT": 2}[o["status"]],
            number(o["total"])
            if o.get("total") is not None and "INVALID_PRICE" not in o["reasons"]
            else Decimal("Infinity"),
            o["hotel_id"],
            o["room_id"],
            o["rate_code"],
        ),
    )


class PublicData:
    supports_supplier_router = True

    def __init__(self, state, network=None):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.network = network
        self.lock = threading.RLock()
        self.count = 0
        self.opener = deadline_http.opener(NoRedirect(), context=verified_context())
        self.dispatch_journal = SupplierDispatchJournal(self.state)
        self._request_local = threading.local()
        self._cleanup_at = 0
        self.cleanup_payloads()

    def cleanup_payloads(self, at=None):
        at = time.time() if at is None else at
        with self.lock:
            if at - self._cleanup_at < 60:
                return
            cleanup_public(self.state, at)
            self._cleanup_at = at

    @contextmanager
    def request_scope(self, owner, request_id):
        # Each incoming authenticated HTTP request gets deterministic supplier step tags.
        previous = getattr(self._request_local, "scope", None)
        self._request_local.scope = [owner, request_id, 0]
        try:
            yield
        finally:
            self._request_local.scope = previous

    def call(self, kind, payload=None, params=None, ttl=0):
        deadline.check()
        self.cleanup_payloads()
        supplier_kind = kind in (
            "liteapi_catalog",
            "liteapi_rates",
            "liteapi_detail",
            "treg_discovery",
        )
        extra_headers = {}
        if kind in ENDPOINTS:
            url = "https://api.tourmind.com/skill/toc/" + ENDPOINTS[kind]
        elif kind == "photon":
            url = "https://photon.komoot.io/api/?" + urllib.parse.urlencode(params)
        elif kind == "weather":
            url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
        elif kind == "fx":
            url = "https://open.er-api.com/v6/latest/CNY"
        elif supplier_kind:
            from supplier_router import endpoint

            url, extra_headers = endpoint(kind)
            if params:
                url += "?" + urllib.parse.urlencode(params)
        else:
            raise DataError("READONLY_ENDPOINT_REFUSED")
        if payload and any(
            k in payload for k in ("user_key", "guest_name", "contact_email", "payment_method")
        ):
            raise DataError("PERSONAL_OR_ORDER_FIELDS_REFUSED")
        fingerprint = hashlib.sha256(canonical([kind, payload, params]).encode()).hexdigest()
        cache = self.state / (fingerprint + ".json")
        failed = self.state / (fingerprint + ".failed.json")
        with self.lock:
            if failed.exists():
                error = json.loads(failed.read_text(encoding="utf-8"))
                if time.time() < error["retry_after_unix"]:
                    raise DataError("UPSTREAM_COOLDOWN_NO_RETRY")
            if ttl and cache.exists():
                rec = json.loads(cache.read_text(encoding="utf-8"))
                if 0 <= time.time() - rec["unix"] < ttl:
                    return rec
            if self.count >= 100:
                raise DataError("PUBLIC_CALL_LIMIT")
            self.count += 1
        # Durable reservation BEFORE network. A crash leaves RESERVED and cannot
        # authorize a retry of the same outbound step or unknown same-owner payload.
        deadline.check()
        dispatch_id = None
        scope = getattr(self._request_local, "scope", None)
        if scope is not None:
            owner, request_id, step = scope
            scope[2] += 1
            try:
                dispatch_id = self.dispatch_journal.reserve(
                    owner, request_id, step, kind, fingerprint
                )
            except ValueError as error:
                raise DataError(str(error)) from error
        rec = {
            "id": __import__("uuid").uuid4().hex,
            "source": url,
            "operation": kind,
            "request": payload or params,
            "observed_at": now(),
            "unix": time.time(),
            "cache_ttl_seconds": ttl,
        }
        start = time.monotonic()
        dispatched = False
        try:
            deadline.check()
            if self.network:
                dispatched = True
                data = self.network(kind, payload, params)
            else:
                raw = canonical(payload).encode() if payload is not None else None
                req = urllib.request.Request(
                    url,
                    data=raw,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "TravelTech-local-readonly/1.0",
                        **extra_headers,
                    },
                )
                socket_timeout = deadline.timeout(55)
                dispatched = True
                with self.opener.open(req, timeout=socket_timeout) as response:
                    rec["http"] = response.status
                    raw = deadline.read(response, 4000001)
                    if len(raw) > 4000000:
                        raise DataError("BODY_TOO_LARGE")
                    if any(value and value.encode() in raw for value in extra_headers.values()):
                        raise DataError("SUPPLIER_SECRET_ECHO")
                    data = strict_json(raw)
            if supplier_kind and (not isinstance(data, dict) or data.get("error")):
                raise DataError("SUPPLIER_ERROR")
            provider_empty = (
                kind == "search"
                and type(data) is dict
                and data.get("ok") is False
                and data.get("error") == "no hotels found with available rates"
            )
            if kind in ENDPOINTS and (
                type(data) is not dict or (data.get("ok") is not True and not provider_empty)
            ):
                raise DataError("SUPPLIER_ERROR")
            rec["data"] = data
            rec["status"] = "PROVIDER_EMPTY" if provider_empty else "OK"
        except Exception as e:
            rec.update(
                status="ERROR",
                outbound_dispatched=dispatched,
                error=type(e).__name__
                + ":"
                + (str(e) if isinstance(e, DataError) else "upstream unavailable; not retried"),
            )
            if isinstance(e, urllib.error.HTTPError):
                rec["http"] = e.code
        rec["latency_sec"] = round(time.monotonic() - start, 3)
        with self.lock:
            with (self.state / "public_receipts.jsonl").open("a", encoding="utf-8") as f:
                f.write(canonical(receipt_metadata(rec)) + "\n")
                f.flush()
                os.fsync(f.fileno())
            if dispatch_id is not None:
                self.dispatch_journal.finish(
                    dispatch_id,
                    rec["status"] in ("OK", "PROVIDER_EMPTY"),
                    rec.get("data"),
                    rec.get("error", "UPSTREAM_ERROR").split(":", 1)[0],
                    dispatched=dispatched,
                )
            if rec["status"] in ("OK", "PROVIDER_EMPTY") and ttl:
                cache.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            if dispatched and rec["status"] not in ("OK", "PROVIDER_EMPTY"):
                failed.write_text(
                    json.dumps({"retry_after_unix": time.time() + 60, "error": rec["error"]}),
                    encoding="utf-8",
                )
        if not dispatched:
            deadline.check()
        if rec["status"] not in ("OK", "PROVIDER_EMPTY"):
            raise DataError("SUPPLIER_UPSTREAM_UNKNOWN_NO_RETRY")
        return rec

    def location(self, keyword):
        if not isinstance(keyword, str) or not 1 <= len(keyword) <= 150:
            raise DataError("LOCATION_INVALID")
        rec = self.call("location", {"keyword": keyword}, ttl=86400)
        data = rec["data"]["data"]
        return {
            "regions": data.get("regions") or [],
            "place": data.get("place"),
            "source": rec["source"],
            "observed_at": rec["observed_at"],
            "receipt_id": rec["id"],
        }

    def geo(self, keyword):
        if not isinstance(keyword, str) or not 1 <= len(keyword) <= 150:
            raise DataError("LOCATION_INVALID")
        rec = self.call("photon", params={"q": keyword, "limit": 5}, ttl=86400)
        return {
            "status": "POI_ONLY_NO_INVENTORY",
            "attribution": "© OpenStreetMap contributors / Photon by komoot; public demo, no SLA",
            "observed_at": rec["observed_at"],
            "features": rec["data"].get("features", []),
        }

    def fx(self):
        try:
            rec = self.call("fx", ttl=1800)
            d = rec["data"]
            updated = d["time_last_update_unix"]
            age = time.time() - updated
            if (
                d.get("result") != "success"
                or d.get("base_code") != "CNY"
                or not isinstance(updated, (int, float))
                or isinstance(updated, bool)
                or not math.isfinite(updated)
                or not 0 <= age <= 172800
            ):
                raise DataError("FX_STALE")
            if not isinstance(d.get("rates"), dict):
                raise DataError("FX_RATE_INVALID")
            candidate = {
                "verified": True,
                "source": "https://www.exchangerate-api.com",
                "endpoint": rec["source"],
                "observed_at": rec["observed_at"],
                "data_updated_at": d["time_last_update_utc"],
                "rates": {
                    k: str(d["rates"][k])
                    for k in ("RUB", "USD", "EUR", "CNY", "GBP", "JPY", "KWD")
                    if k in d["rates"]
                },
            }
            if any(validated_fx_rate(candidate, k) is None for k in ("RUB", "USD", "EUR", "CNY")):
                raise DataError("FX_RATE_INVALID")
            return candidate
        except (DataError, KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            return {
                "verified": False,
                "reason": "CURRENT_FX_UNAVAILABLE; upstream price bounds omitted; CNY retained",
            }

    def weather(self, lat, lon, checkin, checkout):
        if (
            type(lat) not in (int, float)
            or type(lon) not in (int, float)
            or not math.isfinite(lat + lon)
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
        ):
            raise DataError("COORDINATES_INVALID")
        start = date.fromisoformat(checkin)
        end = date.fromisoformat(checkout)
        today = datetime.now(timezone.utc).date()
        if not today <= start < end <= today + timedelta(days=16):
            return {
                "status": "OUTSIDE_FORECAST_HORIZON",
                "source": "Open-Meteo",
                "forecast_available": False,
                "observed_at": now(),
                "requested_dates": [checkin, checkout],
            }
        rec = self.call(
            "weather",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "start_date": checkin,
                "end_date": (end - timedelta(days=1)).isoformat(),
            },
            ttl=1800,
        )
        return {
            "status": "OK",
            "source": rec["source"],
            "observed_at": rec["observed_at"],
            "provider_data_timestamp": None,
            "provider_data_timestamp_note": "Response gives forecast dates and retrieval time, not model initialization time",
            "attribution": "Weather data by Open-Meteo.com, CC BY 4.0; free noncommercial API",
            "forecast_available": True,
            "daily": rec["data"]["daily"],
            "units": rec["data"]["daily_units"],
        }

    @staticmethod
    def stay(request):
        start = date.fromisoformat(request["checkin"])
        end = date.fromisoformat(request["checkout"])
        if start < datetime.now(timezone.utc).date() or not 1 <= (end - start).days <= 90:
            raise DataError("FUTURE_DATES_REQUIRED")
        rooms = request["rooms"]
        if (
            not isinstance(rooms, list)
            or not 1 <= len(rooms) <= 4
            or any(r != rooms[0] for r in rooms)
        ):
            raise DataError("MIXED_ROOM_OCCUPANCY_UNSUPPORTED")
        r = rooms[0]
        adults = r["adults"]
        children = r.get("children", [])
        if (
            type(adults) is not int
            or not 1 <= adults <= 8
            or len(children) > 4
            or any(type(x) is not int or not 0 <= x <= 17 for x in children)
        ):
            raise DataError("OCCUPANCY_INVALID")
        number(request["budget"])
        return {
            "check_in_date": start.isoformat(),
            "check_out_date": end.isoformat(),
            "adults": adults,
            "room_count": len(rooms),
            "children": len(children),
            "children_ages": children,
        }

    def search(self, request, location):
        from supplier_router import active_supplier, search as supplier_search

        if active_supplier() != "TOURMIND":
            return supplier_search(self, request, location)
        return self._search_tourmind(request, location)

    def _search_tourmind(self, request, location):
        stay = self.stay(request)
        if not isinstance(location, dict):
            raise DataError("RESOLVED_LOCATION_REQUIRED")
        mode = location.get("mode", "region")
        if mode == "region":
            if (
                set(location) != {"mode", "region_id", "location_name"}
                or not isinstance(location["region_id"], str)
                or not location["region_id"].isdigit()
            ):
                raise DataError("REGION_INVALID")
            loc = {k: location[k] for k in ("region_id", "location_name")}
        elif mode == "nearby":
            if set(location) != {"mode", "latitude", "longitude", "radius_km", "location_name"}:
                raise DataError("NEARBY_SCHEMA")
            if (
                not all(
                    type(location[k]) in (int, float) and math.isfinite(location[k])
                    for k in ("latitude", "longitude", "radius_km")
                )
                or not -90 <= location["latitude"] <= 90
                or not -180 <= location["longitude"] <= 180
                or not 0 < location["radius_km"] <= 20
            ):
                raise DataError("NEARBY_RANGE")
            loc = {k: v for k, v in location.items() if k != "mode"}
        else:
            raise DataError("LOCATION_MODE")
        # No upstream price bounds: original currency is kept, exact local verified FX only.
        rec = self.call("search", {**stay, **loc}, ttl=120)
        if rec["status"] == "PROVIDER_EMPTY":
            return {
                "status": "NO_LIVE_OFFERS",
                "source": "TourMind public read-only",
                "observed_at": rec["observed_at"],
                "candidate_count": 0,
                "candidate_signals_are_not_quotes": True,
                "offers": [],
                "failures": [],
                "fx": None,
                "request": request,
                "location": location,
                "availability_verified": False,
                "booking_performed": False,
                "search_scope": "EXPLICIT_SUPPLIER_NO_RATES",
                "supplier_reason_code": "NO_HOTELS_WITH_AVAILABLE_RATES",
                "receipt_id": rec["id"],
            }
        data = rec["data"]["data"]
        candidates = data.get("hotels") or data.get("hotel_list") or []
        if not isinstance(candidates, list):
            raise DataError("SUPPLIER_SCHEMA")
        ids = list(
            dict.fromkeys(str(x["hotel_id"]) for x in candidates if x.get("hotel_id") is not None)
        )[:20]
        offers = []
        failures = []
        fx = self.fx() if request["currency"] != "CNY" else None
        if ids:
            rates = self.call("batch", {**stay, "hotel_ids": ids}, ttl=120)
            results = rates["data"]["data"].get("results", [])
            if not isinstance(results, list):
                raise DataError("BATCH_SCHEMA_INVALID")
            byid = {str(x["hotel_id"]): x for x in candidates}
            requested = set(ids)
            counts = {}
            for x in results:
                if isinstance(x, dict):
                    key = str(x.get("hotel_id", ""))
                    counts[key] = counts.get(key, 0) + 1
            got = set()
            for item in results:
                if not isinstance(item, dict):
                    failures.append({"hotel_id": "", "status": "INVALID_BATCH_ITEM"})
                    continue
                hotel_id = str(item.get("hotel_id", ""))
                if hotel_id not in requested:
                    failures.append({"hotel_id": hotel_id, "status": "UNEXPECTED_BATCH_HOTEL_ID"})
                    continue
                got.add(hotel_id)
                if counts.get(hotel_id, 0) != 1:
                    failures.append({"hotel_id": hotel_id, "status": "DUPLICATE_BATCH_HOTEL_ID"})
                    continue
                if item.get("ok") is not True:
                    failures.append(
                        {
                            "hotel_id": hotel_id,
                            "status": "UNKNOWN_UPSTREAM_FAILURE",
                            "reason": item.get("reason", item.get("error")),
                        }
                    )
                    continue
                payload = item.get("data") or {}
                if not isinstance(payload, dict):
                    failures.append({"hotel_id": hotel_id, "status": "INVALID_BATCH_ITEM"})
                    continue
                rows = self.normalize_rates(
                    payload, hotel_id, byid[hotel_id], request, rates["observed_at"]
                )
                offers.extend(rows)
                if not rows:
                    failures.append(
                        {
                            "hotel_id": hotel_id,
                            "status": "NO_MATCHING_LIVE_ROOM",
                            "reason": payload.get("reason"),
                        }
                    )
            failures.extend(
                {"hotel_id": x, "status": "MISSING_BATCH_ITEM"} for x in ids if x not in got
            )
        verified = verify_offers(offers, request, fx)
        return {
            "status": "PARTIAL" if failures else "OK" if offers else "NO_LIVE_OFFERS",
            "source": "TourMind public read-only",
            "observed_at": rec["observed_at"],
            "candidate_count": len(candidates),
            "candidate_signals_are_not_quotes": True,
            "offers": verified,
            "failures": failures,
            "fx": fx,
            "request": request,
            "location": location,
            "availability_verified": False,
            "booking_performed": False,
            "search_scope": data.get("search_scope"),
            "receipt_id": rec["id"],
            "coverage": {
                "complete_city": False,
                "searched_hotels": len(ids),
                "candidate_hotels": len(candidates),
                "hotel_limit": 20,
                "truncated": len(candidates) > len(ids),
            },
        }

    @staticmethod
    def normalize_rates(data, hotel_id, hotel, request, stamp):
        offers = []
        for room in data.get("room_types", []):
            for product in room.get("products", []):
                rate = product.get("rate") or {}
                code = rate.get("rate_code")
                room_id = room.get("room_id", room.get("room_type_code"))
                if not code or room_id is None:
                    continue
                try:
                    amount = str(number(rate["total_price"]))
                except (KeyError, DataError):
                    amount = None
                # Typed supplier-only facts; unspecified amenities stay UNKNOWN.
                facts = {}
                # Retain method-specific totals as evidence; never infer tax completeness.
                fees = {}
                for field, method in (("stripe_payment_fee", "STRIPE"),):
                    raw_fee = rate.get(field)
                    if isinstance(raw_fee, dict):
                        try:
                            fee_amount = number(raw_fee.get("fee_amount"))
                            payable = number(raw_fee.get("payable_amount"))
                            base = number(rate.get("total_price"))
                            if (
                                raw_fee.get("currency") == rate.get("currency")
                                and payable == base + fee_amount
                            ):
                                fees[method] = {
                                    "currency": raw_fee["currency"],
                                    "fee_amount": str(fee_amount),
                                    "payable_amount": str(payable),
                                    "field": field,
                                }
                        except DataError:
                            pass
                if fees:
                    facts["payment_fees"] = fees
                for field in ("taxes", "fees", "tax_data"):
                    if isinstance(rate.get(field), (dict, list)):
                        facts[field + "_evidence"] = rate[field]
                star = hotel.get("star_rating")
                if type(star) is int and 1 <= star <= 5:
                    facts["star_rating"] = {
                        "value": star,
                        "field": "hotel.star_rating",
                        "source": "TOURMIND_SEARCH",
                    }
                distance = hotel.get("distance_km")
                if (
                    type(distance) in (int, float)
                    and math.isfinite(distance)
                    and 0 <= distance <= 1000
                ):
                    facts["distance_to_search_anchor_km"] = {
                        "value": round(distance, 2),
                        "field": "hotel.distance_km",
                        "source": "TOURMIND_SEARCH",
                    }
                meal = product.get("meal_count")
                if type(meal) is int and meal >= 0:
                    facts["meal_count"] = {
                        "value": meal,
                        "field": "product.meal_count",
                        "source": "TOURMIND_RATE",
                        "note": "Meal count is NOT a breakfast guarantee",
                    }
                offers.append(
                    HotelOffer(
                        "TOURMIND_LIVE_RATE",
                        stamp,
                        str(hotel_id),
                        hotel.get("hotel_name", hotel.get("name", "Hotel " + hotel_id)),
                        str(room_id),
                        room.get("room_name", room.get("name", "Room " + str(room_id))),
                        str(code),
                        request["checkin"],
                        request["checkout"],
                        request["rooms"],
                        len(request["rooms"]),
                        amount,
                        rate.get("currency", "UNKNOWN"),
                        cancellation=product.get("cancellation_policy"),
                        capacity=product.get("max_occupancy")
                        if type(product.get("max_occupancy")) is int
                        else None,
                        breakfast=False if product.get("meal_count") == 0 else None,
                        on_request=rate.get("is_on_request"),
                        supplier_facts=facts,
                    )
                )
        return offers

    def enrich_details(self, result, requirements=None):
        from hotel_details import enrich

        return enrich(self, result, requirements or {})

    def availability(self, offer, request):
        stay = self.stay(request)
        if offer.get("source") != "TOURMIND_LIVE_RATE" or not offer.get("rate_code"):
            raise DataError("LIVE_RATE_REQUIRED")
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(offer["observed_at"])
            if not timedelta(0) <= age <= timedelta(minutes=5):
                raise DataError("QUOTE_STALE_SEARCH_AGAIN")
        except (ValueError, TypeError, KeyError):
            raise DataError("QUOTE_TIMESTAMP_INVALID")
        if (
            offer.get("check_in"),
            offer.get("check_out"),
            offer.get("occupancy"),
            offer.get("room_count"),
        ) != (request["checkin"], request["checkout"], request["rooms"], len(request["rooms"])):
            raise DataError("QUOTE_REQUEST_CONTEXT_MISMATCH")
        rec = self.call(
            "availability", {**stay, "hotel_id": offer["hotel_id"], "rate_code": offer["rate_code"]}
        )
        data = rec["data"]["data"]
        return self.normalize_availability(data, offer, stay, rec["observed_at"])

    @staticmethod
    def normalize_availability(data, offer, stay, stamp):
        expected_dates = [
            (date.fromisoformat(stay["check_in_date"]) + timedelta(days=n)).isoformat()
            for n in range(
                (
                    date.fromisoformat(stay["check_out_date"])
                    - date.fromisoformat(stay["check_in_date"])
                ).days
            )
        ]
        checked = []
        for room in data.get("room_types", []):
            for rate in room.get("rateInfos", []):
                price = rate.get("totalPrice")
                daily = rate.get("dailyPrices", [])
                try:
                    total = number(price)
                except DataError:
                    continue
                observed_dates = [x.get("stayDate") for x in daily]
                capacity = rate.get("maxOccupancy")
                allotment = rate.get("allotment")
                bound = rate.get("rateCode") == offer["rate_code"]
                identity_ok = any(data.get(k) is not None for k in ("hotel_id", "hotelId")) and any(
                    room.get(k) is not None for k in ("roomTypeCode", "room_id", "room_type_code")
                )
                for obj, fields, expected in (
                    (data, ("hotel_id", "hotelId"), offer["hotel_id"]),
                    (room, ("roomTypeCode", "room_id", "room_type_code"), offer["room_id"]),
                    (rate, ("roomTypeCode", "room_id", "room_type_code"), offer["room_id"]),
                ):
                    if any(obj.get(k) is not None and str(obj[k]) != str(expected) for k in fields):
                        identity_ok = False
                currency = rate.get("currencyCode")
                currency_ok = (
                    isinstance(currency, str) and re.fullmatch("[A-Z]{3}", currency) is not None
                )
                if (
                    offer.get("currency")
                    and offer["currency"] != "UNKNOWN"
                    and currency != offer["currency"]
                ):
                    currency_ok = False
                try:
                    amounts = [number(x.get("price")) for x in daily]
                    price_ok = (
                        len(amounts) == len(expected_dates) and sum(amounts, Decimal("0")) == total
                    )
                    price_ok = price_ok and all(
                        x.get("currencyCode", currency) == currency for x in daily
                    )
                except (DataError, TypeError, AttributeError):
                    price_ok = False
                try:
                    price_unchanged = number(offer.get("total")) == total
                except (DataError, TypeError):
                    price_unchanged = False
                child_limit = rate.get("maxChildren")
                children_ok = (child_limit is None and stay["children"] == 0) or (
                    type(child_limit) is int and 0 <= stay["children"] <= child_limit
                )
                prior_policy = offer.get("cancellation") or {}
                new_policy = {
                    "refundable": rate.get("refundable"),
                    "cancelPolicyInfos": rate.get("cancelPolicyInfos"),
                }
                cancellation_changed = (
                    type(prior_policy.get("effective_non_refundable")) is bool
                    and type(rate.get("refundable")) is bool
                    and prior_policy["effective_non_refundable"] == rate["refundable"]
                )
                if (
                    prior_policy.get("cancelPolicyInfos") is not None
                    and new_policy["cancelPolicyInfos"] is not None
                ):
                    cancellation_changed = cancellation_changed or canonical(
                        prior_policy["cancelPolicyInfos"]
                    ) != canonical(new_policy["cancelPolicyInfos"])
                currency_changed = isinstance(currency, str) and currency != offer.get("currency")
                quote_valid = (
                    bound
                    and identity_ok
                    and price_ok
                    and children_ok
                    and isinstance(currency, str)
                    and re.fullmatch("[A-Z]{3}", currency) is not None
                    and observed_dates == expected_dates
                )
                available = (
                    quote_valid
                    and currency_ok
                    and price_unchanged
                    and not cancellation_changed
                    and type(allotment) is int
                    and allotment >= stay["room_count"]
                    and type(capacity) is int
                    and capacity >= stay["adults"] + stay["children"]
                    and rate.get("isOnRequest") is False
                )
                checked.append(
                    {
                        "rate_code": rate.get("rateCode"),
                        "room_name": rate.get("roomTypeName"),
                        "requested_room_name": offer["room_name"],
                        "room_name_consistent": rate.get("roomTypeName") == offer["room_name"],
                        "identity_verified": identity_ok,
                        "price_unchanged": price_unchanged,
                        "currency_changed": currency_changed,
                        "cancellation_changed": cancellation_changed,
                        "quote_recheck_verified": bool(quote_valid),
                        "room_binding_note": "Exact rate code, hotel and room identities are required. Changed terms require separate consent.",
                        "total": str(price),
                        "currency": rate.get("currencyCode"),
                        "allotment": allotment,
                        "capacity": capacity,
                        "daily_dates": observed_dates,
                        "rate_binding": bound,
                        "available_at_observation": available,
                        "cancellation": new_policy,
                        "taxes_known": None,
                    }
                )
        matching = [x for x in checked if x["rate_binding"] and x["identity_verified"]]
        if len(matching) > 1:
            # One checked rate must identify exactly one offer; duplicate observations
            # cannot be used to cherry-pick an available row or a new price.
            for row in matching:
                row["available_at_observation"] = False
                row["quote_recheck_verified"] = False
        verified = any(x["available_at_observation"] for x in checked)
        price_changed = any(
            x["rate_binding"]
            and x["identity_verified"]
            and (not x["price_unchanged"] or x["currency_changed"])
            for x in checked
        )
        terms_changed = any(
            x["rate_binding"] and x["identity_verified"] and x["cancellation_changed"]
            for x in checked
        )
        return {
            "price_changed": price_changed,
            "terms_changed": terms_changed,
            "requires_user_reacceptance": price_changed or terms_changed,
            "payable_price_confirmed": False,
            "source": "TourMind check_room_availability",
            "observed_at": stamp,
            "hotel_id": offer["hotel_id"],
            "room_id": offer["room_id"],
            "requested_rate_code": offer["rate_code"],
            "request": stay,
            "snapshot": data,
            "checked_rates": checked,
            "status": "AVAILABLE_RATE_AT_OBSERVATION"
            if verified
            else "PRICE_CHANGED_REVIEW"
            if price_changed
            else "AVAILABILITY_NOT_ESTABLISHED",
            "availability_verified": verified,
            "booking_performed": False,
            "caveat": "Read-only time-bound rate snapshot: exact rate, dated nights, occupancy and allotment. Room label may change or room ID may be omitted by check; no reservation, taxes and final availability can change.",
        }
