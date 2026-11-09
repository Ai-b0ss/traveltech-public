"""Identity-bound hotel content and explicit fee disclosures, not final quotes.

Sources: official TourMind parameter_guide and LiteAPI displaying-hotel-details.
Facility-list absence is unknown. Descriptions never become amenity evidence.
Static fee periods are used only when the provider explicitly supplies a basis.
"""

import copy, math, re
from hotel_location_media import extract
from html.parser import HTMLParser


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("p", "li", "br", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def text(value):
    if not isinstance(value, str):
        return ""
    parser = _Text()
    parser.feed(value[:20000])
    return "\n".join(" ".join(x.split()) for x in "".join(parser.parts).splitlines() if x.strip())


from decimal import Decimal, InvalidOperation

FACILITIES = {
    "pool": {
        "pool",
        "swimming pool",
        "indoor pool",
        "outdoor pool",
        "indoor swimming pool",
        "outdoor swimming pool",
        "бассейн",
    },
    "parking": {
        "parking",
        "free parking",
        "private parking",
        "parking on site",
        "parking onsite",
        "парковка",
        "бесплатная парковка",
    },
    "air_conditioning": {
        "air conditioning",
        "air-conditioning",
        "кондиционер",
        "кондиционирование",
    },
    # Soundproof rooms is not a hotel-wide promise of quietness.
    "quiet": {"quiet rooms", "тихие номера"},
}


def money(value):
    if type(value) not in (int, float, str, Decimal):
        return None
    try:
        d = Decimal(str(value))
        return d if d.is_finite() and 0 <= d <= Decimal("999999999999") else None
    except (ValueError, InvalidOperation):
        return None


def parse_detail(raw, hotel_id, provider, stamp):
    if not isinstance(raw, dict):
        raise ValueError("HOTEL_DETAIL_SCHEMA")
    detail = raw.get("hotel", raw) if provider == "TOURMIND_DETAIL" else raw
    keys = ("hotel_id", "hotelId") if provider == "TOURMIND_DETAIL" else ("id", "hotelId")
    identities = [str(detail[k]) for k in keys if detail.get(k) is not None]
    if not identities or any(i != str(hotel_id) for i in identities):
        raise ValueError("HOTEL_DETAIL_IDENTITY_MISMATCH")
    facts = {}
    star = detail.get("star_rating") if provider == "TOURMIND_DETAIL" else detail.get("starRating")

    def fact(key, value, field):
        facts[key] = {
            "value": value,
            "source": provider,
            "field": field,
            "observed_at": stamp,
            "hotel_id": str(hotel_id),
        }

    if type(star) in (int, float) and math.isfinite(star) and 1 <= star <= 5:
        fact(
            "star_rating",
            star,
            "hotel.star_rating" if provider == "TOURMIND_DETAIL" else "data.starRating",
        )
    amenities = (
        detail.get("amenities", detail.get("amenities_hotel"))
        if provider == "TOURMIND_DETAIL"
        else detail.get("hotelFacilities")
    )
    if isinstance(amenities, dict):
        for key in FACILITIES:
            if type(amenities.get(key)) is bool:
                fact(key, amenities[key], "hotel.amenities." + key)
    lists = (
        [("hotel.amenities_hotel" if "amenities_hotel" in detail else "hotel.amenities", amenities)]
        if provider == "TOURMIND_DETAIL"
        else [("data.hotelFacilities", amenities), ("data.facilities", detail.get("facilities"))]
    )
    for field, items in lists:
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items[:500]):
            label = (
                item
                if isinstance(item, str)
                else item.get("name")
                if isinstance(item, dict)
                else None
            )
            if not isinstance(label, str):
                continue
            normalized = " ".join(label.casefold().strip().split())
            # Exact canonical facility names only; no substring "no pool" / "pool nearby".
            for key, names in FACILITIES.items():
                if normalized in names and key not in facts:
                    fact(key, True, field + "[" + str(index) + "]")
    times = detail.get("checkinCheckoutTimes") or {} if provider == "LITEAPI_DETAIL" else {}
    evidence = {
        "hotel_id": str(hotel_id),
        "source": provider,
        "observed_at": stamp,
        "address": text(detail.get("address")),
        **extract(detail, provider),
        "checkin": detail.get("checkin", times.get("checkin")),
        "checkout": detail.get("checkout", times.get("checkout")),
        "amenities": copy.deepcopy(amenities),
        "facilities": copy.deepcopy(detail.get("facilities")),
        "fees": copy.deepcopy(detail.get("fees")),
        "taxes": copy.deepcopy(detail.get("taxes")),
        "fee_disclosures": [
            text(str(row.get("name", row.get("description", "Сбор"))))
            + "; "
            + text(str(row.get("amount", "сумма не указана")))
            + " "
            + text(str(row.get("currency", "")))
            + "; "
            + text(str(row.get("basis", "период начисления не указан")))
            + "; "
            + text(str(row.get("conditions", "")))
            for field in ("fees", "taxes")
            for row in (detail.get(field) if isinstance(detail.get(field), list) else [])
            if isinstance(row, dict)
        ],
        "important_information": detail.get("hotelImportantInformation", detail.get("policies")),
        "static_content_not_availability": True,
        "amenity_names": [
            text(x if isinstance(x, str) else x.get("name", ""))
            for x in (amenities if isinstance(amenities, list) else [])
            if isinstance(x, (str, dict))
        ],
        "mandatory_fee_text": text((detail.get("fees") or {}).get("mandatory"))
        if isinstance(detail.get("fees"), dict)
        else "",
        "optional_fee_text": text((detail.get("fees") or {}).get("optional"))
        if isinstance(detail.get("fees"), dict)
        else "",
        "room_catalog_amenities_not_exact_room": copy.deepcopy(detail.get("amenities_room")),
    }
    for key in ("checkin", "checkout"):
        value = evidence.get(key)
        evidence[key + "_text"] = (
            "; ".join(
                text(str(v))
                for k, v in value.items()
                if k
                in (
                    "begin_time",
                    "end_time",
                    "time",
                    "instructions",
                    "special_instructions",
                    "min_age",
                )
            )
            if isinstance(value, dict)
            else text(value)
        )
    evidence["important_information_text"] = text(evidence.get("important_information"))
    return facts, evidence


def fees_for_stay(evidence, request, currency):
    """Normalize static disclosures without inventing an amount, basis or FX.

    Rate fees remain authoritative. Static amounts form an alternative lower bound,
    never an additive second copy of a charge already present in a rate breakdown.
    """
    charges = []
    unpriced = []
    totals = {}
    nights = (
        __import__("datetime").date.fromisoformat(request["checkout"])
        - __import__("datetime").date.fromisoformat(request["checkin"])
    ).days
    rooms = len(request["rooms"])
    people = sum(r["adults"] + len(r.get("children", [])) for r in request["rooms"])
    multipliers = {
        "per_stay": 1,
        "per_room_per_stay": rooms,
        "per_room_per_night": rooms * nights,
        "per_person_per_stay": people,
        "per_person_per_night": people * nights,
    }
    for category in ("fees", "taxes"):
        raw = evidence.get(category)
        if raw is None:
            continue
        items = raw if isinstance(raw, list) else [raw]
        for item in items[:100]:
            row = {
                "category": category,
                "raw": copy.deepcopy(item),
                "source": evidence["source"],
                "hotel_id": evidence["hotel_id"],
            }
            if not isinstance(item, dict):
                unpriced.append(row)
                continue
            d = money(item.get("amount"))
            cur = item.get("currency")
            basis = item.get("basis")
            included = item.get("included")
            row.update(
                included=included if type(included) is bool else None, currency=cur, basis=basis
            )
            # Explicit conditions can depend on age, dates or residency; do not guess them.
            deposit = (
                bool(
                    re.search(
                        r"deposit|депозит|залог",
                        str(item.get("type", "")) + " " + str(item.get("name", "")),
                        re.I,
                    )
                )
                or item.get("refundable") is True
            )
            conditional = deposit or any(
                item.get(k) not in (None, False, "", [])
                for k in ("conditions", "age_range", "nationality", "optional")
            )
            if (
                type(included) is not bool
                or d is None
                or not isinstance(cur, str)
                or not re.fullmatch("[A-Z]{3}", cur)
                or basis not in multipliers
                or conditional
            ):
                unpriced.append(row)
                continue
            total = d * multipliers[basis]
            row["stay_amount"] = format(total, "f")
            charges.append(row)
            if included is False:
                totals[cur] = totals.get(cur, Decimal("0")) + total
    return {
        "charges": charges,
        "unpriced_disclosures": unpriced,
        "known_excluded_by_currency": {k: format(v, "f") for k, v in totals.items()},
        "known_excluded_in_quote_currency": format(totals.get(currency, Decimal("0")), "f"),
        "complete": False,
        "payable_price_confirmed": False,
    }


def enrich(public, result, requirements):
    from hotel_requirements import check

    result = copy.deepcopy(result)
    offers = result.get("offers", [])
    eligible = [
        o
        for o in offers
        if o.get("status") != "REJECT"
        and o.get("source")
        in ("TOURMIND_LIVE_RATE", "LITEAPI_SANDBOX_RATE", "LITEAPI_RATE_OBSERVATION")
    ]
    ids = list(dict.fromkeys((o["source"], o["hotel_id"]) for o in eligible))
    limit = 20 if requirements else 5
    targets = ids[:limit]
    failures = result.setdefault("detail_failures", [])
    for source, hid in targets:
        pending = [o for o in offers if o["hotel_id"] == hid and o.get("source") == source]
        provider = "TOURMIND_DETAIL" if source == "TOURMIND_LIVE_RATE" else "LITEAPI_DETAIL"
        try:
            rec = (
                public.call("detail", {"hotel_id": hid}, ttl=86400)
                if provider == "TOURMIND_DETAIL"
                else public.call(
                    "liteapi_detail", params={"hotelId": hid, "language": "en"}, ttl=86400
                )
            )
            raw = rec["data"].get("data") if isinstance(rec["data"], dict) else None
            facts, evidence = parse_detail(raw, hid, provider, rec["observed_at"])
            for o in pending:
                prior = o.setdefault("supplier_facts", {})
                conflicts = set(o.get("hotel_fact_conflicts", []))
                for key, entry in facts.items():
                    if key in conflicts:
                        continue
                    if key in prior and prior[key].get("value") != entry["value"]:
                        prior.pop(key, None)
                        conflicts.add(key)
                        o.setdefault("reasons", []).append("SUPPLIER_FACT_CONFLICT")
                    else:
                        prior[key] = entry
                o["hotel_fact_conflicts"] = sorted(conflicts)
                o["hotel_detail_evidence"] = copy.deepcopy(evidence)
                costs = fees_for_stay(evidence, result["request"], o.get("currency"))
                o["cost_evidence"] = costs
                if costs["unpriced_disclosures"]:
                    o.setdefault("reasons", []).append("PROPERTY_FEES_REQUIRE_REVIEW")
                rate_fee = prior.get("known_excluded_total") or {}
                rate_value = (
                    money(rate_fee.get("amount"))
                    if rate_fee.get("currency") == o.get("currency")
                    else None
                )
                static_value = money(costs["known_excluded_in_quote_currency"])
                lower = max(rate_value or Decimal("0"), static_value or Decimal("0"))
                prior["known_excluded_total"] = {
                    "amount": format(lower, "f"),
                    "currency": o.get("currency"),
                    "source": provider,
                    "combination": "MAX_ALTERNATIVE_LOWER_BOUNDS_NOT_ADDITIVE",
                }
        except (ValueError, KeyError, TypeError, AttributeError):
            failures.append({"hotel_id": hid, "source": provider, "status": "DETAIL_UNVERIFIED"})
    result["detail_coverage"] = {
        "hotel_limit": limit,
        "attempted_hotels": len(targets),
        "candidate_hotels": len(ids),
        "complete_candidate_pool": len(targets) == len(ids),
        "complete_city": False,
    }
    return result
