"""Read-only LiteAPI native rates. No fallback city, invented price or booking.

Contract: docs.liteapi.travel/docs/hotel-rates-api-json-data-structure
Each roomTypes offer contains one rate per requested occupancy. Totals are
summed once across those rooms; returned currencies and identities are retained.
"""

import copy, hashlib, json, os, re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from provider_transport import canonical
from live_data import DataError

BASE = "https://api.liteapi.travel/v3.0"
KEY_FILE = Path.home() / ".traveltech-secrets" / "hotel_keys.json"


def api_key():
    value = os.environ.get("LITEAPI_KEY")
    if value:
        return value
    # Owner-provided sandbox key; never enumerate/rotate accounts or print keys.
    path = Path(os.environ.get("LITEAPI_KEYS_FILE", str(KEY_FILE)))
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("sandbox_key")
        except (OSError, ValueError):
            pass
    return None


def amount(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d < 0 or d > Decimal("999999999999"):
            return None
        return format(d, "f")
    except (ValueError, InvalidOperation):
        return None


def build_request(request, hotel_ids, guest_nationality):
    from app import request_from

    request_from(request)
    if not isinstance(guest_nationality, str) or not re.fullmatch("[A-Z]{2}", guest_nationality):
        raise DataError("GUEST_NATIONALITY_REQUIRED")
    if (
        not hotel_ids
        or len(hotel_ids) > 20
        or any(not isinstance(x, str) or not x or len(x) > 256 for x in hotel_ids)
    ):
        raise DataError("HOTEL_IDENTITIES_REQUIRED")
    return {
        "hotelIds": hotel_ids,
        "checkin": request["checkin"],
        "checkout": request["checkout"],
        "currency": request["currency"],
        "guestNationality": guest_nationality,
        "occupancies": [
            {"adults": r["adults"], "children": list(r.get("children", []))}
            for r in request["rooms"]
        ],
        "roomMapping": True,
        "maxRatesPerHotel": 10,
    }


def normalize(payload, request, catalog, stamp, *, sandbox=True):
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise DataError("SUPPLIER_RATES_SCHEMA")
    if len(rows) > 100:
        raise DataError("SUPPLIER_TOO_MANY_HOTELS")
    source = "LITEAPI_SANDBOX_RATE" if sandbox else "LITEAPI_RATE_OBSERVATION"
    offers = []
    failures = []
    for hotel in rows:
        if not isinstance(hotel, dict):
            raise DataError("SUPPLIER_HOTEL_SCHEMA")
        hid = hotel.get("hotelId")
        if not isinstance(hid, str) or hid not in catalog:
            failures.append({"status": "UNEXPECTED_BATCH_HOTEL_ID"})
            continue
        meta = catalog[hid]
        groups = hotel.get("roomTypes")
        if not isinstance(groups, list):
            raise DataError("SUPPLIER_ROOM_GROUP_SCHEMA")
        for group in groups[:100]:
            if not isinstance(group, dict):
                raise DataError("SUPPLIER_ROOM_GROUP_SCHEMA")
            rates = group.get("rates")
            offer_token = group.get("offerId")
            if not isinstance(rates, list) or len(rates) != len(request["rooms"]):
                failures.append({"hotel_id": hid, "status": "ROOM_ALLOCATION_UNVERIFIED"})
                continue
            if not isinstance(offer_token, str) or not 1 <= len(offer_token) <= 10000:
                failures.append({"hotel_id": hid, "status": "RATE_IDENTITY_MISSING"})
                continue
            ordered = {}
            bad = False
            for rate in rates:
                if not isinstance(rate, dict):
                    bad = True
                    break
                slot = rate.get("occupancyNumber", 1 if len(rates) == 1 else None)
                if type(slot) is not int or not 1 <= slot <= len(rates) or slot in ordered:
                    bad = True
                    break
                ordered[slot] = rate
            if bad or len(ordered) != len(rates):
                failures.append({"hotel_id": hid, "status": "ROOM_ALLOCATION_UNVERIFIED"})
                continue
            rates = [ordered[i + 1] for i in range(len(rates))]
            reasons = []
            currencies = []
            prices = []
            breakfast = []
            capacities = []
            excluded = Decimal("0")
            tax_evidence = []
            native_ids = []
            policies = []
            provider_occupancy = []
            for rate, party in zip(rates, request["rooms"]):
                token = rate.get("rateId")
                if not isinstance(token, str) or not 1 <= len(token) <= 10000:
                    bad = True
                    break
                native_ids.append(token)
                retail = rate.get("retailRate") or {}
                totals = retail.get("total")
                total = (
                    totals[0]
                    if isinstance(totals, list) and len(totals) == 1 and isinstance(totals[0], dict)
                    else {}
                )
                val = amount(total.get("amount"))
                cur = total.get("currency")
                if val is None:
                    reasons.append("PRICE_UNKNOWN")
                else:
                    prices.append(Decimal(val))
                if not isinstance(cur, str) or not re.fullmatch("[A-Z]{3}", cur):
                    cur = "UNKNOWN"
                    reasons.append("CURRENCY_UNKNOWN")
                currencies.append(cur)
                context = {
                    "adultCount": rate.get("adultCount"),
                    "childCount": rate.get("childCount"),
                    "childrenAges": rate.get("childrenAges"),
                }
                provider_occupancy.append(context)
                for field, expected in [
                    ("adultCount", party["adults"]),
                    ("childCount", len(party.get("children", []))),
                ]:
                    if rate.get(field) is not None and (
                        type(rate[field]) is not int or rate[field] != expected
                    ):
                        reasons.append("REQUEST_CONTEXT_MISMATCH")
                if rate.get("childrenAges") is not None and rate["childrenAges"] != party.get(
                    "children", []
                ):
                    reasons.append("REQUEST_CONTEXT_MISMATCH")
                if (
                    context["adultCount"] is None
                    or context["childCount"] is None
                    or (party.get("children") and context["childrenAges"] is None)
                ):
                    reasons.append("SUPPLIER_OCCUPANCY_UNVERIFIED")
                board = rate.get("boardType")
                breakfast.append(
                    True if board in ("BB", "BI") else False if board == "RO" else None
                )
                cap = rate.get("maxOccupancy")
                capacities.append(cap if type(cap) is int and cap > 0 else None)
                if type(cap) is int and cap < party["adults"] + len(party.get("children", [])):
                    reasons.append("CAPACITY_EXCEEDED")
                policies.append(copy.deepcopy(rate.get("cancellationPolicies")))
                taxes = retail.get("taxesAndFees", rate.get("taxesAndFees"))
                tax_evidence.append(copy.deepcopy(taxes))
                if isinstance(taxes, list):
                    for tax in taxes:
                        if not isinstance(tax, dict):
                            continue
                        if tax.get("included") is False:
                            fee = amount(tax.get("amount"))
                            if fee is not None and tax.get("currency") == cur:
                                excluded += Decimal(fee)
                            else:
                                reasons.append("EXCLUDED_FEES_UNPRICED")
            if bad:
                failures.append({"hotel_id": hid, "status": "RATE_IDENTITY_MISSING"})
                continue
            if len(set(currencies)) != 1:
                reasons.append("MIXED_RATE_CURRENCIES")
            cur = currencies[0] if len(set(currencies)) == 1 else "UNKNOWN"
            total = (
                format(sum(prices, Decimal("0")), "f")
                if len(prices) == len(rates) and cur != "UNKNOWN"
                else None
            )
            if cur != request["currency"]:
                reasons.append("FX_NOT_AVAILABLE")
            reasons.extend(
                [
                    "SUPPLIER_DATES_REQUEST_BOUND",
                    "TAX_COVERAGE_UNVERIFIED",
                    "AVAILABILITY_NOT_CHECKED",
                    "FINAL_PAYABLE_PRICE_UNVERIFIED",
                ]
            )
            if sandbox:
                reasons.append("SANDBOX_NOT_LIVE_INVENTORY")
            if (
                total is not None
                and cur == request["currency"]
                and Decimal(total) + excluded > Decimal(request["budget"])
            ):
                reasons.append("KNOWN_TOTAL_OVER_BUDGET")
            identity = hashlib.sha256(
                canonical({"hotel": hid, "offer": offer_token, "rates": native_ids}).encode()
            ).hexdigest()
            rooms = hashlib.sha256(
                canonical(
                    [
                        {"occupancy": i + 1, "mapped": r.get("mappedRoomId"), "rate": native_ids[i]}
                        for i, r in enumerate(rates)
                    ]
                ).encode()
            ).hexdigest()
            facts = {}
            star = meta.get("stars")
            if type(star) in (int, float) and 1 <= star <= 5:
                facts["star_rating"] = {
                    "field": "hotel.star_rating",
                    "value": star,
                    "source": "LITEAPI_CATALOG",
                }
            facts["taxes_evidence"] = tax_evidence
            facts["known_excluded_total"] = {
                "amount": format(excluded, "f"),
                "currency": cur,
                "source": "LITEAPI_RATE",
            }
            facts["cancellation_evidence"] = policies
            offers.append(
                {
                    "source": source,
                    "source_kind": "SANDBOX" if sandbox else "PROVIDER_RATE",
                    "observed_at": stamp,
                    "hotel_id": hid,
                    "hotel_name": meta.get("name") or "Отель " + hid,
                    "room_id": rooms,
                    "room_name": " / ".join(
                        str(r.get("name") or "Номер без названия") for r in rates
                    ),
                    "rate_code": identity,
                    "check_in": request["checkin"],
                    "check_out": request["checkout"],
                    "occupancy": copy.deepcopy(request["rooms"]),
                    "provider_occupancy": provider_occupancy,
                    "room_count": len(rates),
                    "total": total,
                    "currency": cur,
                    "price_basis": "FULL_STAY_ALL_ROOMS",
                    "taxes_known": None,
                    "breakfast": False
                    if False in breakfast
                    else True
                    if all(x is True for x in breakfast)
                    else None,
                    "capacity": capacities[0] if len(capacities) == 1 else None,
                    "room_capacities": capacities,
                    "cancellation": {
                        "effective_non_refundable": any(
                            isinstance(x, dict) and x.get("refundableTag") == "NRFN"
                            for x in policies
                        ),
                        "supplier_policies": policies,
                    },
                    "supplier_facts": facts,
                    "context_binding": "REQUEST_BOUND_NOT_PROVIDER_VERIFIED",
                    "reasons": list(dict.fromkeys(reasons)),
                    "status": "REJECT"
                    if any(
                        x in reasons
                        for x in [
                            "REQUEST_CONTEXT_MISMATCH",
                            "MIXED_RATE_CURRENCIES",
                            "KNOWN_TOTAL_OVER_BUDGET",
                            "CAPACITY_EXCEEDED",
                        ]
                    )
                    else "REVIEW",
                    "availability_verified": False,
                    "booking_confirmation_available": False,
                }
            )
    return {
        "source": source,
        "source_kind": "SANDBOX" if sandbox else "PROVIDER_RATE",
        "supplier": "LiteAPI",
        "request": copy.deepcopy(request),
        "observed_at": stamp,
        "offers": offers,
        "failures": failures,
        "status": "PARTIAL" if failures else "OK" if offers else "NO_LIVE_OFFERS",
        "availability_verified": False,
        "booking_performed": False,
        "coverage": {"complete_city": False, "searched_hotels": len(catalog), "hotel_limit": 20},
    }


def search(public, request):
    from supplier_router import nationality as get_nationality

    nationality = get_nationality()
    if not nationality:
        raise DataError("GUEST_NATIONALITY_REQUIRED")
    rec = public.call(
        "liteapi_catalog",
        params={"countryCode": request["country"], "cityName": request["city"], "limit": 20},
        ttl=86400,
    )
    rows = rec["data"].get("data") if isinstance(rec["data"], dict) else None
    if not isinstance(rows, list):
        raise DataError("SUPPLIER_CATALOG_SCHEMA")
    catalog = {}
    for row in rows[:20]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        # Returned contradictory geography is never relabelled as the requested city.
        country = row.get("countryCode", row.get("country"))
        city = row.get("cityName", row.get("city"))
        if country:
            from city_names import COUNTRIES

            if not isinstance(country, str) or country.casefold() not in (
                request["country"].casefold(),
                COUNTRIES[request["country"]].casefold(),
            ):
                continue
        if city and (not isinstance(city, str) or city.casefold() != request["city"].casefold()):
            continue
        catalog[row["id"]] = row
    if not catalog:
        return {
            "source": "LITEAPI_SANDBOX_RATE"
            if os.environ.get("LITEAPI_ENVIRONMENT", "sandbox") != "production"
            else "LITEAPI_RATE_OBSERVATION",
            "source_kind": "SANDBOX"
            if os.environ.get("LITEAPI_ENVIRONMENT", "sandbox") != "production"
            else "PROVIDER_RATE",
            "request": copy.deepcopy(request),
            "observed_at": rec["observed_at"],
            "offers": [],
            "failures": [],
            "status": "NO_LIVE_OFFERS",
            "coverage": {"complete_city": False, "searched_hotels": 0},
            "availability_verified": False,
            "booking_performed": False,
        }
    body = build_request(request, list(catalog), nationality)
    rates = public.call("liteapi_rates", body)
    result = normalize(
        rates["data"],
        request,
        catalog,
        rates["observed_at"],
        sandbox=os.environ.get("LITEAPI_ENVIRONMENT", "sandbox") != "production",
    )
    result["guest_nationality"] = nationality
    return result
