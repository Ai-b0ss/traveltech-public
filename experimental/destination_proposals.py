"""Isolated MOCK destination-proposal contract; no live claims or HTTP calls."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

_ALLOWED = {
    "warm_sea": {"seasonal_estimate", "confirmed_by_source", "unknown"},
    "short_flight": {"confirmed_by_distance", "unknown"},
    "budget_fit": {"confirmed_by_source", "unknown"},
    "quiet": {"not_applicable_at_destination_level", "unknown"},
}
_DEFAULT_PREFERENCES = {
    "warm_sea": "unknown",
    "short_flight": "unknown",
    "budget_fit": "unknown",
    "quiet": "not_applicable_at_destination_level",
}
_MOCK_CITIES = ("Сочи", "Анталия", "Батуми")


def propose_destinations(soft_preferences=None, *, limit=3):
    if type(limit) is not int or limit not in (2, 3):
        raise ValueError("DESTINATION_LIMIT_MUST_BE_2_OR_3")
    if soft_preferences is not None and not isinstance(soft_preferences, dict):
        raise ValueError("SOFT_PREFERENCES_INVALID")
    requested = soft_preferences or {}
    if not requested:
        return {
            "status": "needs_input",
            "destinations": [],
            "soft_preferences": dict(_DEFAULT_PREFERENCES),
            "questions": ["Из какого города вылетаете?"],
        }
    unknown = set(requested) - set(_ALLOWED)
    if unknown:
        raise ValueError("SOFT_PREFERENCE_UNKNOWN:" + ",".join(sorted(unknown)))
    for name, value in requested.items():
        if not isinstance(value, str):
            raise ValueError("SOFT_PREFERENCE_INVALID_TYPE:" + name)
        if value == "confirmed_by_source":
            raise ValueError("UNTRUSTED_SOURCE:" + name)
        if value not in _ALLOWED[name]:
            raise ValueError("SOFT_PREFERENCE_STATUS_INVALID:" + name)
    preferences = {**_DEFAULT_PREFERENCES, **requested}
    destinations = [
        {
            "city": city,
            "reason": "MOCK-кандидат для проверки сценария направления; соответствие, доступность и погода не проверялись.",
            "flight_time_minutes": None,
            "total_price": None,
            "currency": None,
            "source_mode": "MOCK",
            "warnings": [
                "Демонстрационный кандидат, не ответ поставщика и не подтверждённая рекомендация.",
                "Цена, валюта и время перелёта неизвестны; null не означает ноль.",
            ],
        }
        for city in _MOCK_CITIES[:limit]
    ]
    return {
        "status": "ok",
        "destinations": destinations,
        "soft_preferences": preferences,
        "questions": [],
    }


def _money(value, error_code):
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise ValueError(error_code)
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(error_code) from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(error_code)
    return amount


def _money_text(amount):
    return format(amount.quantize(Decimal("0.01")), "f")


def _validated_budget(budget):
    if not isinstance(budget, dict):
        raise ValueError("BUDGET_INVALID")
    amount = _money(budget.get("amount"), "BUDGET_INVALID")
    scope = budget.get("scope")
    if scope not in ("whole_trip", "hotel_only"):
        raise ValueError("BUDGET_SCOPE_REQUIRED")
    currency = budget.get("currency")
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise ValueError("BUDGET_CURRENCY_INVALID")
    normalized = deepcopy(budget)
    normalized["amount"] = _money_text(amount)
    return normalized, amount, currency, scope


def _date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("DATE_INVALID") from None


def _date_pair(start, end):
    first, last = _date(start), _date(end)
    if last < first:
        raise ValueError("DATE_RANGE_INVALID")
    return first, last


def shift_dates(plan, days=2):
    if not isinstance(plan, dict):
        raise ValueError("TRIP_PLAN_INVALID")
    if type(days) is not int or days == 0 or abs(days) > 365:
        raise ValueError("DATE_SHIFT_INVALID")
    delta = timedelta(days=days)
    patch = {
        "source_refresh_required": True,
        "invalidated_prices": ["hotel.amount", "transport.amount", "transfer.amount"],
        "source_revision": plan.get("revision"),
    }
    if "start_date" in plan or "end_date" in plan:
        if "start_date" not in plan or "end_date" not in plan:
            raise ValueError("DATE_FIELDS_REQUIRED")
        first, last = _date_pair(plan["start_date"], plan["end_date"])
        patch.update(
            start_date=(first + delta).isoformat(),
            end_date=(last + delta).isoformat(),
        )
    if "checkin" in plan or "checkout" in plan:
        if "checkin" not in plan or "checkout" not in plan:
            raise ValueError("DATE_FIELDS_REQUIRED")
        first, last = _date_pair(plan["checkin"], plan["checkout"])
        patch.update(
            checkin=(first + delta).isoformat(),
            checkout=(last + delta).isoformat(),
        )
    hotel = plan.get("hotel")
    if hotel is not None:
        if not isinstance(hotel, dict):
            raise ValueError("HOTEL_INVALID")
        if "checkin" in hotel or "checkout" in hotel:
            if "checkin" not in hotel or "checkout" not in hotel:
                raise ValueError("DATE_FIELDS_REQUIRED")
            first, last = _date_pair(hotel["checkin"], hotel["checkout"])
            patch["hotel"] = {
                "checkin": (first + delta).isoformat(),
                "checkout": (last + delta).isoformat(),
            }
    if not any(key in patch for key in ("start_date", "end_date", "checkin", "checkout", "hotel")):
        raise ValueError("DATE_FIELDS_REQUIRED")
    return patch


def reduce_budget(budget, percent=25):
    normalized, amount, _, _ = _validated_budget(budget)
    if type(percent) not in (int, str) or isinstance(percent, bool):
        raise ValueError("BUDGET_PERCENT_INVALID")
    try:
        factor = Decimal("1") - Decimal(str(percent)) / Decimal("100")
    except (InvalidOperation, ValueError):
        raise ValueError("BUDGET_PERCENT_INVALID") from None
    if not factor.is_finite() or not 0 < factor < 1:
        raise ValueError("BUDGET_PERCENT_INVALID")
    normalized["amount"] = _money_text(amount * factor)
    return normalized


def evaluate_budget(budget, total_price, *, price_currency=None, price_scope="whole_trip"):
    """Compare a confirmed price only with a budget of the SAME scope/currency.

    The positional value remains a whole-trip price by default for compatibility.
    Passing a hotel price requires explicit price_scope="hotel_only"; it is not a
    trip total. Unknown scope/currency never produces a false ok/infeasible.
    """
    normalized, budget_amount, currency, budget_scope = _validated_budget(budget)

    def unknown(code, question):
        return {
            "status": "unknown",
            "total_price": None,
            "budget": normalized,
            "questions": [question],
            "warnings": [code],
        }

    label = ("стоимость отеля" if budget_scope == "hotel_only"
             else "полная стоимость поездки, включая отель, транспорт и трансфер")
    if total_price is None:
        return unknown("PRICE_UNKNOWN", f"Какова {label}?")
    amount = _money(total_price, "PRICE_INVALID")
    if price_scope not in ("whole_trip", "hotel_only"):
        raise ValueError("PRICE_SCOPE_INVALID")
    if price_scope != budget_scope:
        return unknown("PRICE_SCOPE_MISMATCH",
                       f"Нужна {label}: стоимость другой части поездки несопоставима с бюджетом.")
    if price_currency is None:
        return unknown("PRICE_CURRENCY_UNKNOWN", "В какой валюте указана цена?")
    if not isinstance(price_currency, str) or price_currency != currency:
        raise ValueError("CURRENCY_MISMATCH")

    status = "infeasible" if amount > budget_amount else "ok"
    return {
        "status": status,
        "total_price": _money_text(amount) if price_scope == "whole_trip" else None,
        "hotel_price": _money_text(amount) if price_scope == "hotel_only" else None,
        "price_scope": price_scope,
        "budget": normalized,
        "questions": [],
        "warnings": ["TOTAL_PRICE_EXCEEDS_BUDGET"] if status == "infeasible" else [],
    }


__all__ = [
    "propose_destinations",
    "shift_dates",
    "reduce_budget",
    "evaluate_budget",
]
