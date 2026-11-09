"""Bounded, supplier-evidence-only, currency-safe hotel diversification.
No inferred quality, swimming pools, beach distance, hotel stars or availability.
"""

from decimal import Decimal, InvalidOperation
from collections import defaultdict

STATES = {"CONSTRAINTS_MET_SNAPSHOT": 0, "REVIEW": 1, "REJECT": 2}


def decimal_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except (ValueError, InvalidOperation):
        return None


def comparable_price(offer, currency):
    native = decimal_or_none(offer.get("_price"))
    if native is None:
        return None
    if offer.get("_currency") == currency:
        return native
    fx = offer.get("fx") or {}
    if offer.get("_currency") == "CNY":
        from live_data import validated_fx_rate

        rate = validated_fx_rate(fx, currency)
        if rate is not None:
            return native * rate
    return None


def verified_condition_score(offer, request, preferences_text=""):
    """Rank only hard facts known to the supplier, not unverified wishes."""
    reasons = set(offer.get("_reasons") or ())
    score = 0
    if request.breakfast:
        if offer.get("breakfast") is True:
            score += 2
        if offer.get("breakfast") is False:
            score -= 3
    if offer.get("taxes_known") is True:
        score += 1
    if offer.get("capacity") is not None and "CAPACITY_EXCEEDED" not in reasons:
        score += 1
    if offer.get("cancellation") and isinstance(offer["cancellation"], dict):
        score += 1
    # Extra ranking must be backed by typed supplier fields and explicit user words.
    import re

    match = re.search(
        r"(?:от|не ниже|минимум)\s*([1-5])\s*зв[её]зд|([1-5])\s*зв[её]зд", preferences_text, re.I
    )
    stars = (offer.get("supplier_facts") or {}).get("star_rating") or {}
    if match and stars.get("field") == "hotel.star_rating" and type(stars.get("value")) is int:
        requested = int(match.group(1) or match.group(2))
        score += 3 if stars["value"] >= requested else -3
    # Do NOT boost offer.availability_verified for the transient search snapshot.
    return score


def sort_key(offer, request, preferences_text=""):
    currency = str(request.currency)
    comparable = comparable_price(offer, currency)
    return (
        STATES.get(offer.get("status"), 3),
        comparable is None,
        -verified_condition_score(offer, request, preferences_text),
        comparable if comparable is not None else Decimal("Infinity"),
        str(offer.get("_currency") or ""),
        str(offer.get("_price") or ""),
        str(offer.get("_key") or ""),
    )


def shortlist(offers, request, max_count=24, preferences_text=""):
    if not isinstance(max_count, int) or not 1 <= max_count <= 100:
        raise ValueError("INVALID_SHORTLIST_LIMIT")
    ranked = sorted(offers, key=lambda x: sort_key(x, request, preferences_text))
    byhotel = defaultdict(list)
    for offer in ranked:
        hotel = str(offer.get("hotel_id") or offer.get("_key") or "")
        byhotel[hotel].append(offer)
    # Each hotel's best verified candidate competes on the same sorting contract.
    first = [items[0] for items in byhotel.values()]
    first.sort(key=lambda x: sort_key(x, request, preferences_text))
    selected = first[:max_count]
    selected_ids = {id(x) for x in selected}
    if len(selected) < max_count:
        rest = [x for x in ranked if id(x) not in selected_ids]
        selected.extend(rest[: max_count - len(selected)])
    return selected
