"""Offline checks for an already assembled TripPlan.

The accepted contract and cart stay where they are. This module only answers
whether the components can be shown, whether their sum is a verified trip total,
and whether a demo cart replay belongs to the same owner. It does not call a
provider, take payment, or write the session store.
"""

import copy
from datetime import datetime

try:
    from .mock_cart import CartError, CartSnapshot
    from .trip_plan import TripPlanError, assemble_trip_plan, budget_status, stale_offer
except ImportError:
    from mock_cart import CartError, CartSnapshot
    from trip_plan import TripPlanError, assemble_trip_plan, budget_status, stale_offer


CONDITION_CHANGES = frozenset({"dates", "guests", "budget", "airport", "pet"})
REQUIRED = ("hotel", "transport", "transfer")


def inspect_trip_plan(source, expected_revision=None):
    """Return a view of the plan and a separate verified total.

    A missing full-stay basis, tax flag, live observation, or transport price
    leaves verified_total empty. The component amounts stay visible.
    """
    _travellers(source)
    plan = assemble_trip_plan(source, expected_revision=expected_revision)
    issues = [dict(item) for item in plan["warnings"] if item["code"] != "PRICE_UNKNOWN"]
    _return_flight(plan, issues)
    _price_rules(plan, issues)
    verified = None if issues else plan["total_amount"]
    issues.extend(dict(item) for item in plan["warnings"] if item["code"] == "PRICE_UNKNOWN")
    return {
        "plan": plan,
        "viewable": True,
        "known_total": plan["total_amount"],
        "verified_total": verified,
        "budget_fits": _budget_fits(plan, verified),
        "issues": issues,
        "payment": "NOT_PROCESSED",
    }


def refresh_offer(plan, current_revision):
    """Withdraw dated prices when the session revision moved. Not a new fare."""
    return stale_offer(plan, current_revision)


def apply_condition_change(plan, change):
    """Drop hotel, flight, and transfer prices after a confirmed condition edit.

    The revision number stays. The caller has not built a replacement fare.
    """
    if change not in CONDITION_CHANGES:
        raise TripPlanError("CHANGE")
    source = copy.deepcopy(plan)
    for name in REQUIRED:
        source[name]["amount"] = None
        source[name]["fare_status"] = "STALE"
    assembled = assemble_trip_plan(source)
    assembled["warnings"].append({
        "code": "STALE_REVISION",
        "component": "transport",
        "message": (
            "Условия изменились. Цены отеля, перелёта и трансфера сняты. Это не новый тариф."
        ),
    })
    return assembled


def save_demo_cart(store, plan, *, owner, cart_id, revision):
    snapshot = CartSnapshot(plan, owner=owner, cart_id=cart_id, expected_revision=revision)
    return store.save_cart(snapshot, expected_revision=revision)


def place_demo_order(store, *, owner, cart_id, revision, idempotency_key):
    order = store.process(
        owner=owner,
        cart_id=cart_id,
        expected_revision=revision,
        idempotency_key=idempotency_key,
    )
    if order.get("payment_status") != "NOT_PROCESSED" or order.get("booking_status") != "NOT_BOOKED":
        raise TripPlanError("PAYMENT")
    return order


def _travellers(source):
    party = source.get("travellers") if isinstance(source, dict) else None
    children = party.get("children") if isinstance(party, dict) else None
    adults = party.get("adults") if isinstance(party, dict) else None
    if (
        type(adults) is not int
        or adults < 1
        or not isinstance(children, list)
        or any(type(age) is not int or age < 0 or age > 17 for age in children)
    ):
        raise TripPlanError("TRAVELLERS")


def _return_flight(plan, issues):
    transport = plan["transport"]
    departure = transport.get("return_departure_at")
    arrival = transport.get("arrival_at")
    if departure and arrival and _instant(departure) < _instant(arrival):
        issues.append({
            "code": "RETURN_BEFORE_OUTBOUND",
            "component": "transport",
            "message": "Обратный вылет раньше прибытия туда.",
        })


def _instant(value):
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _price_rules(plan, issues):
    for name in REQUIRED:
        item = plan[name]
        if item.get("fare_status") == "STALE":
            _issue(issues, "STALE_REVISION", name, "Цена снята и не переносится в новые условия.")
        if item.get("amount") == "0.00" and item.get("price_policy") != "FREE":
            _issue(issues, "ZERO_PRICE", name, "Нулевая цена без отметки FREE не входит в проверенный итог.")
        basis = item.get("price_basis")
        if basis == "NIGHTLY":
            _issue(issues, "NIGHTLY_NOT_GROUP", name, "Цена за ночь не является суммой проживания.")
        elif basis == "PER_PERSON":
            _issue(issues, "PER_PERSON_NOT_GROUP", name, "Цена за человека не является суммой группы.")
        elif name == "hotel" and basis != "FULL_STAY":
            _issue(issues, "PRICE_BASIS_UNKNOWN", name, "Нет полной стоимости проживания. Итог не подтверждён.")
        if name == "hotel" and item.get("taxes_included") is not True:
            _issue(issues, "TAXES_UNKNOWN", name, "Налоги проживания не подтверждены.")
        if item.get("amount") is not None and item.get("source_mode") != "LIVE":
            _issue(issues, "SOURCE_NOT_LIVE", name, "Источник не LIVE. Сумма не является живым тарифом.")
        if item.get("source_mode") == "LIVE" and not item.get("observed_at"):
            _issue(issues, "OBSERVED_AT_UNKNOWN", name, "Нет времени наблюдения живого тарифа.")


def _issue(issues, code, component, message):
    issues.append({"code": code, "component": component, "message": message})


def _budget_fits(plan, verified):
    if plan["budget"]["amount"] is None or plan["budget"]["scope"] == "unspecified":
        return None
    status = budget_status(plan)
    if status is False:
        return False
    if verified is None:
        return None
    return status
