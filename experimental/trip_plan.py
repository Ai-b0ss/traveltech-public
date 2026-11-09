"""Собрать один TripPlan. Сессия и поиск рейсов здесь не вызываются.

Для карточки Ильи:
- функция `assemble_trip_plan(source, expected_revision=None)`;
- образец `docs/contracts/trip_plan_v0.json`;
- чтение демо с локального сервера: `GET /api/trip_plan/demo`;
- отрисовка: `renderTripPlanCard` в `experimental/trip_plan_card.js`.

Живой Ignav не вызывается. Ключ, остаток бесплатной квоты и права на этот
процесс не подтверждены, поэтому демо остаётся MOCK и пустая выдача не
заполняется выдуманными рейсами.
"""

import copy
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

class TripPlanError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


REQUIRED = ("hotel", "transport", "transfer")
OPTIONAL = ("insurance",)
SCOPES = frozenset({"whole_trip", "hotel_only", "unspecified"})
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OFFSET_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:Z|[+-](?:0\d|1[0-4]):[0-5]\d)$"
)
UNKNOWN_PRICE = {
    "insurance": "Цена страховки неизвестна. В итог она не входит: это не 0 и не замена другим тарифом.",
    "hotel": "Цена жилья неизвестна. В итог она не входит: это не 0 и не замена другим тарифом.",
    "transport": "Цена перелёта неизвестна. В итог она не входит: это не 0 и не замена другим тарифом.",
    "transfer": "Цена трансфера неизвестна. В итог она не входит: это не 0 и не замена другим тарифом.",
}


def parse_amount(value):
    """Decimal for a known price. None stays None and is not a fare."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise TripPlanError("AMOUNT_TYPE")
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise TripPlanError("AMOUNT_TYPE") from error
    if not amount.is_finite():
        raise TripPlanError("AMOUNT_TYPE")
    try:
        cents = amount.quantize(Decimal("0.01"))
    except InvalidOperation as error:
        raise TripPlanError("AMOUNT_TYPE") from error
    if amount < 0 or amount != cents or cents > Decimal("999999999999.99"):
        raise TripPlanError("AMOUNT_TYPE")
    return cents


def require_date(value):
    if not isinstance(value, str) or DATE.fullmatch(value) is None:
        raise TripPlanError("DATE")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise TripPlanError("DATE") from error


def require_offset(value):
    if not isinstance(value, str) or OFFSET_TIME.fullmatch(value) is None:
        raise TripPlanError("TIME_OFFSET")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise TripPlanError("TIME_OFFSET")


def assemble_trip_plan(source, expected_revision=None):
    """Вернуть новый словарь TripPlan либо выбросить TripPlanError.

    `total_amount` считается заново. Бюджет в сумму не входит.
    Неизвестная цена остаётся null. Разные валюты не складываются.
    """
    if not isinstance(source, dict):
        raise TripPlanError("PLAN_TYPE")
    plan = copy.deepcopy(source)
    revision = plan.get("revision")
    if type(revision) is not int or revision < 0:
        raise TripPlanError("REVISION")
    if expected_revision is not None and revision != expected_revision:
        raise TripPlanError("STALE_REVISION")
    require_date(plan.get("start_date"))
    require_date(plan.get("end_date"))
    if plan["start_date"] > plan["end_date"]:
        raise TripPlanError("DATE")
    currency = plan.get("currency")
    if not isinstance(currency, str) or len(currency) != 3:
        raise TripPlanError("CURRENCY")
    _budget(plan, currency)
    for name in REQUIRED:
        _component(plan.get(name), name, currency, required=True)
    if plan.get("insurance") is not None:
        _component(plan["insurance"], "insurance", currency, required=False)
    _hotel_dates(plan["hotel"])
    _flight_times(plan["transport"])
    _transfer(plan["transfer"])
    plan["total_amount"] = _total(plan, currency)
    plan["warnings"] = _warnings(plan)
    return plan


def budget_status(plan):
    """True, если лимит покрывает свой scope. None, если сравнить нельзя.

    `hotel_only` смотрит только на жильё. `whole_trip` смотрит на итог.
    Неизвестный итог не считается экономией.
    """
    budget = plan["budget"]
    if budget["amount"] is None or budget["scope"] == "unspecified":
        return None
    if budget["currency"] != plan["currency"]:
        return None
    limit = parse_amount(budget["amount"])
    if budget["scope"] == "hotel_only":
        cost = parse_amount(plan["hotel"]["amount"])
    else:
        cost = parse_amount(plan["total_amount"])
    if cost is None:
        return None
    return cost <= limit


def _budget(plan, currency):
    budget = plan.get("budget")
    if not isinstance(budget, dict) or budget.get("scope") not in SCOPES:
        raise TripPlanError("BUDGET_SCOPE")
    amount = parse_amount(budget.get("amount"))
    if amount is None and budget["scope"] != "unspecified":
        raise TripPlanError("BUDGET_SCOPE")
    if budget.get("currency") != currency and amount is not None:
        raise TripPlanError("MIXED_CURRENCY")
    if not isinstance(budget.get("currency"), str):
        raise TripPlanError("CURRENCY")


def _component(component, name, currency, required):
    if not isinstance(component, dict):
        raise TripPlanError("COMPONENT")
    amount = parse_amount(component.get("amount"))
    if amount is None and component.get("fare_status") == "AVAILABLE":
        raise TripPlanError("UNKNOWN_FARE")
    if amount is None and name == "insurance":
        component["fare_status"] = component.get("fare_status") or "UNKNOWN"
    if amount is not None and component.get("currency") != currency:
        raise TripPlanError("MIXED_CURRENCY")
    for field in ("currency", "source_mode", "provider"):
        if not isinstance(component.get(field), str) or not component[field]:
            raise TripPlanError("SOURCE")
    if required and amount is None:
        return


def _hotel_dates(hotel):
    require_date(hotel.get("checkin"))
    require_date(hotel.get("checkout"))
    if hotel["checkin"] >= hotel["checkout"]:
        raise TripPlanError("DATE")


def _flight_times(transport):
    for field in ("departure_at", "arrival_at", "return_departure_at", "return_arrival_at"):
        if transport.get(field) is not None:
            require_offset(transport[field])
    if transport.get("amount") is not None and (
        transport.get("departure_at") is None or transport.get("arrival_at") is None
    ):
        raise TripPlanError("TIME_OFFSET")


def _transfer(transfer):
    if transfer.get("fare_scope") != "one_way_arrival":
        raise TripPlanError("TRANSFER_SCOPE")
    duration = transfer.get("duration_minutes")
    if duration is not None and (type(duration) is not int or duration < 0):
        raise TripPlanError("TRANSFER_DURATION")
    if transfer.get("pickup_at") is not None:
        require_offset(transfer["pickup_at"])


def _total(plan, currency):
    total = Decimal("0.00")
    for name in REQUIRED:
        amount = parse_amount(plan[name]["amount"])
        if amount is None:
            return None
        total += amount
    insurance = plan.get("insurance")
    if insurance is not None:
        amount = parse_amount(insurance["amount"])
        if amount is not None:
            if insurance["currency"] != currency:
                raise TripPlanError("MIXED_CURRENCY")
            total += amount
    return f"{total:.2f}"


def _warnings(plan):
    warnings = []
    names = REQUIRED + (("insurance",) if plan.get("insurance") is not None else ())
    for name in names:
        if plan[name].get("amount") is None:
            warnings.append(
                {
                    "code": "PRICE_UNKNOWN",
                    "component": name,
                    "message": UNKNOWN_PRICE[name],
                }
            )
    warnings.extend(_schedule_warnings(plan))
    return warnings


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _schedule_warnings(plan):
    warnings = []
    arrival = plan["transport"].get("arrival_at")
    if arrival is not None:
        # Local date comes from the offset on the timestamp, not from a UTC
        # calendar day and not from comparing the raw strings.
        arrival_day = _instant(arrival).date()
        if arrival_day > date.fromisoformat(plan["hotel"]["checkin"]):
            warnings.append(
                {
                    "code": "CHECKIN_BEFORE_ARRIVAL",
                    "component": "hotel",
                    "message": "Заезд начинается раньше локальной даты прилёта. Такой стык нельзя считать подтверждённым.",
                }
            )
    pickup = plan["transfer"].get("pickup_at")
    if pickup is not None and arrival is not None and _instant(pickup) < _instant(arrival):
        warnings.append(
            {
                "code": "TRANSFER_BEFORE_ARRIVAL",
                "component": "transfer",
                "message": "Трансфер начинается раньше фактического прилёта. Маршрут нельзя подтвердить.",
            }
        )
    return warnings


def _withdraw_dated_prices(plan):
    """Hotel, flight and transfer prices belong to the old dates. This is not a new quote."""
    source = copy.deepcopy(plan)
    for name in REQUIRED:
        source[name]["amount"] = None
        source[name]["fare_status"] = "STALE"
    return source


def stale_offer(plan, current_revision):
    """Fail closed when the saved revision has moved.

    A revision mismatch is not a refreshed quote. Prices that depend on the
    dates — hotel, flight and transfer — are removed together.
    """
    if type(current_revision) is not int or current_revision < 0:
        raise TripPlanError("REVISION")
    if plan.get("revision") == current_revision:
        return assemble_trip_plan(plan, expected_revision=current_revision)
    assembled = assemble_trip_plan(_withdraw_dated_prices(plan))
    assembled["warnings"].append(
        {
            "code": "STALE_REVISION",
            "component": "transport",
            "message": "Ревизия изменилась. Цены отеля, перелёта и трансфера сняты. Это не новый тариф.",
        }
    )
    return assembled


def demo_trip_plan():
    """Accepted Moscow → Dubai document, with the total recomputed."""
    fixture = Path(__file__).resolve().parents[1] / "docs" / "contracts" / "trip_plan_v0.json"
    return assemble_trip_plan(json.loads(fixture.read_text(encoding="utf-8")))


def flight_supply():
    """MOCK demo. A live Ignav call needs a confirmed key, quota and rights."""
    reason = "QUOTA_NOT_CONFIRMED" if os.environ.get("IGNAV_API_KEY") else "API_KEY_REQUIRED"
    return {"source_mode": "MOCK", "reason": reason, "offers": []}
