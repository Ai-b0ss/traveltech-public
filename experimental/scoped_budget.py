"""Keep a whole-trip limit separate from the hotel request budget.

This module does not invent prices, currencies, exchange rates, or availability.
Unknown component prices stay null. Null is not zero and is not a supplier quote.
"""

import re
from decimal import Decimal

from budget_math import formatted, resolve_budget

# Scope markers are bound to individual money mentions. A second unlabeled
# amount is never assigned the scope of the first one.
WHOLE_SCOPE = (
    r"(?:общ(?:ий|его|ему)\s+бюджет(?:\s+поездки)?|"
    r"бюджет\s+(?:(?:на|для)\s+)?(?:(?:всей|всю|весь|всего|нашей|этой)\s+)?"
    r"(?:поездк[уаи]|путешестви[ея]|маршрута?)|"
    r"(?:на|за)\s+(?:(?:всю|весь)\s+)?(?:поездку|маршрут|путешествие)|whole_trip)"
)
HOTEL_SCOPE = (
    r"(?:(?:общий\s+)?бюджет\s+(?:только\s+)?(?:(?:на|для)\s+)?"
    r"(?:отел\w*|гостиниц\w*|проживани\w*)|"
    r"(?:на|за)\s+(?:отел\w*|гостиниц\w*|проживани\w*|номер)|hotel_only)"
)
SCOPE_MARKER = re.compile(
    rf"(?P<hotel>{HOTEL_SCOPE})|(?P<whole>{WHOLE_SCOPE})|(?P<generic>бюджет)", re.I
)
MONEY_CURRENCY = (
    r"(?:рубл\w*|руб\b|RUB\b|₽|доллар\w*|USD\b|евро|EUR\b|€|"
    r"юан\w*|CNY\b|GBP\b|JPY\b|KWD\b)"
)


def initial_scopes(text):
    """Extract stated limits, with null currency when the user did not name it.

    A single generic 'budget' retains the legacy hotel-search meaning. Once
    whole_trip is explicit, every other money amount needs its own scope.
    """
    from budget_math import NUMBER, number
    from natural_language import explicit_currency_in_text

    text = re.sub(r"(?<=\d)\.(?=\s|$)", " ", text)
    markers = list(SCOPE_MARKER.finditer(text))
    entries = []
    for match in re.finditer(NUMBER, text, re.I):
        before = [m for m in markers if m.end() <= match.start()]
        marker = before[-1] if before else None
        gap = text[marker.end():match.start()] if marker else ""
        prefix = bool(marker and re.fullmatch(
            r"[\s:=—–-]*(?:(?:до|на|не более|в размере|составляет|ровно)\s*)?", gap, re.I
        ))
        currency_match = re.match(r"\s*" + MONEY_CURRENCY, text[match.end():], re.I)
        end = match.end() + (currency_match.end() if currency_match else 0)
        suffix = next((m for m in markers if m.start() >= end), None)
        suffixed = bool(suffix and re.fullmatch(r"\s*", text[end:suffix.start()]))
        if not prefix and not currency_match and not suffixed:
            continue
        if not prefix and suffixed:
            marker = suffix
        elif not prefix:
            marker = None
        scope = "whole_trip" if marker and marker.lastgroup == "whole" else (
            "hotel_only" if marker and marker.lastgroup == "hotel" else None
        )
        entries.append({
            "scope": scope, "amount": formatted(number(match)),
            "currency": explicit_currency_in_text(text[match.end():end]),
            "evidence": "USER_STATED",
        })
    has_whole = any(m.lastgroup == "whole" for m in markers)
    active = has_whole or len(entries) > 1
    conflict = active and (
        any(e["scope"] is None or e["amount"] == "INVALID" for e in entries)
        or bool(re.search(r"\b(?:или|либо)\b", text, re.I))
    )
    result = {"active": active, "has_whole": has_whole, "conflict": bool(conflict)}
    for scope in ("whole_trip", "hotel_only"):
        stated = [e for e in entries if e["scope"] == scope]
        if len({(e["amount"], e["currency"]) for e in stated}) > 1:
            result["conflict"] = True
        result[scope] = stated[0] if stated else None
    whole, hotel = result["whole_trip"], result["hotel_only"]
    if (not result["conflict"] and whole and hotel
            and whole["currency"] and whole["currency"] == hotel["currency"]
            and Decimal(hotel["amount"]) > Decimal(whole["amount"])):
        result["conflict"] = True
    return result


def initial_scope_reply(app, text, draft_id):
    """Stop before model/provider dispatch while a new draft has unsafe budgets."""
    from natural_language import local_rules, source_quote_inspection

    if source_quote_inspection(text) is not None:
        return None
    saved = draft_scopes(app, draft_id)
    parsed = initial_scopes(text)
    scopes = saved or {k: parsed[k] for k in ("whole_trip", "hotel_only")}
    whole = scopes.get("whole_trip")
    conflict = scopes.get("conflict") or parsed["conflict"]
    hotel = scopes.get("hotel_only")
    if not conflict and whole and hotel and whole.get("currency") and hotel.get("currency"):
        conflict = conflict or whole["currency"] != hotel["currency"]
        if whole["currency"] == hotel["currency"]:
            conflict = conflict or Decimal(hotel["amount"]) > Decimal(whole["amount"])
    if not conflict and (not whole or (whole.get("currency") and hotel and hotel.get("currency"))):
        return None
    shown = (whole["amount"] + " " + (whole.get("currency") or "(валюта не указана)")) if whole else ""
    message = (
        "Укажите однозначные суммы и валюты отдельно для всей поездки и отеля; "
        "противоречивые или неразмеченные суммы не применены."
        if conflict else
        f"Общий бюджет {shown} " + ("сохранён в черновике" if saved else "указан")
        + " как whole_trip (вся поездка). "
        "Какой бюджет на отель? Укажите сумму и валюту отдельно."
    )
    if whole and not whole.get("currency"):
        message += " Уточните также валюту общего бюджета поездки."
    observed = local_rules(text, None)
    observed = {**observed, "intent": "clarify"}
    observed["needs"] = list(dict.fromkeys(observed.get("needs", []) + [
        "budget_conflict" if conflict else "budget"
    ]))
    return {
        "session_id": None, "request": None, "revision": None, "cards": [],
        "has_preview": False, "source_mode": "UNKNOWN", "language_message": message,
        "pending_budget_scopes": scopes, "whole_trip_confirmation": None,
        "budget_assessment": assessment(whole if whole and whole.get("currency") else None),
        "language": {"source": "LOCAL_SCOPE_RULES", "model_calls": 0,
                     "validated_intent": observed, "state_changed": False},
    }


def draft_scopes(app, draft_id):
    if not draft_id:
        return None
    import json

    with app.lock:
        exists = app.store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_draft_inputs'"
        ).fetchone()
        row = app.store.db.execute(
            "SELECT body FROM local_draft_inputs WHERE id=?", (draft_id,)
        ).fetchone() if exists else None
        return json.loads(row[0]).get("pending_budget_scopes") if row else None


def pending_scope_output(request, saved):
    """Reoffer an unconfirmed initial total only for the unchanged hotel request."""
    scopes = saved.get("pending_budget_scopes") or {}
    whole = scopes.get("whole_trip")
    hotel = scopes.get("hotel_only")
    if (not whole or not hotel or scopes.get("conflict")
            or (saved.get("budget_scopes") or {}).get("whole_trip")
            or hotel.get("amount") != request.budget
            or hotel.get("currency") != request.currency
            or whole.get("currency") != request.currency):
        return {}
    turn = assess("Общий бюджет " + whole["amount"] + " " + whole["currency"], request, None)
    return {"pending_budget_scopes": scopes, "budget_assessment": turn["assessment"],
            "whole_trip_confirmation": turn["confirmation"]}

SEPARATE_WHOLE_TRIP_CONFIRMATION = "SEPARATE_WHOLE_TRIP"
HOTEL_ONLY_CLARIFICATION = (
    "Сохранён бюджет только на отель. Укажите бюджет на отель; "
    "общий бюджет поездки здесь не изменяется."
)

RELAXATIONS = (
    {
        "id": "raise_whole_trip_limit",
        "evidence": "MOCK",
        "price": None,
        "currency": None,
        "text": "Назвать большую сумму лимита whole_trip. Новую цену система не предлагает.",
    },
    {
        "id": "hotel_only_scope",
        "evidence": "UNKNOWN",
        "price": None,
        "currency": None,
        "text": "Сузить запрос до бюджета hotel_only и назвать его отдельно от всего маршрута.",
    },
    {
        "id": "city_and_dates",
        "evidence": "UNKNOWN",
        "price": None,
        "currency": None,
        "text": "Указать город и даты для проверки жилья. Это не цена транспорта, трансфера или всего маршрута.",
    },
)


def hotel_budget_phrase(text):
    return bool(
        re.search(
            r"бюджет\w*(?:\s+\S+){0,6}?\s+(?:на|для)\s+(?:отел\w*|гостиниц\w*|проживани\w*)",
            text,
            re.I,
        )
    )


def _percent(text):
    found = re.findall(r"(\d{1,3})\s*(?:%|процент)", text, re.I)
    if len(found) != 1:
        return None
    value = int(found[0])
    return value if 1 <= value <= 99 else None


def needs_saved_base(text):
    """A relative edit needs an already saved whole-trip amount, never the hotel limit."""
    if re.search(r"\bдо\b|не дороже|ровно|уложись", text, re.I):
        return False
    if _percent(text) is not None:
        return True
    return bool(
        re.search(
            r"(?:уменьш|сниз|сократ|уреж|сбав|опуст|скинь|увелич|повыс|прибав|подним)"
            r".{0,80}?\bна\b",
            text,
            re.I,
        )
    )


def _money(amount):
    if isinstance(amount, bool) or not isinstance(amount, str):
        return None
    try:
        text = formatted(Decimal(amount))
    except Exception:
        return None
    if text in (None, "INVALID"):
        return None
    return text


def apply_percent(amount, percent, currency):
    from destination_proposals import reduce_budget

    reduced = reduce_budget(
        {"amount": amount, "currency": currency, "scope": "whole_trip"},
        percent,
    )
    return formatted(Decimal(reduced["amount"]))


def recompute(patch):
    """Return the signed amount, or None when the patch does not match its own base."""
    if not isinstance(patch, dict):
        return None
    amount = _money(patch.get("amount"))
    if patch.get("op") == "absolute":
        return amount
    if patch.get("op") != "relative" or type(patch.get("percent")) is not int:
        return None
    base = _money(patch.get("scope_base"))
    currency = patch.get("currency")
    if base is None or not isinstance(currency, str):
        return None
    try:
        expected = apply_percent(base, patch["percent"], currency)
    except ValueError:
        return None
    return expected if expected == amount else None


def _components():
    return {
        name: {
            "amount": None,
            "currency": None,
            "evidence": "UNKNOWN",
            "availability": "UNKNOWN",
        }
        for name in ("hotel", "transport", "transfer")
    }


def assessment(limit):
    return {
        "status": "UNKNOWN",
        "reason": "NO_VERIFIED_COMPONENT_PRICES",
        "comparison": "NOT_PERFORMED",
        "whole_trip_limit": limit,
        "components": _components(),
        "availability": "UNKNOWN",
        "relaxations": [dict(item) for item in RELAXATIONS],
        "prices_invented": False,
    }


def public_budget_scopes(request, saved):
    """Hotel limit comes only from the hotel request. Whole-trip is opt-in."""
    hotel = None
    if (
        isinstance(request, dict)
        and isinstance(request.get("budget"), str)
        and isinstance(request.get("currency"), str)
    ):
        hotel = {
            "scope": "hotel_only",
            "amount": request["budget"],
            "currency": request["currency"],
            "evidence": "HOTEL_REQUEST",
        }
    whole = None
    saved_whole = saved.get("whole_trip") if isinstance(saved, dict) else None
    if (
        isinstance(saved_whole, dict)
        and isinstance(saved_whole.get("amount"), str)
        and isinstance(saved_whole.get("currency"), str)
    ):
        whole = {
            "scope": "whole_trip",
            "amount": saved_whole["amount"],
            "currency": saved_whole["currency"],
            "evidence": "USER_STATED",
            "component_prices": "UNKNOWN",
            "availability": "UNKNOWN",
        }
    return {"hotel_only": hotel, "whole_trip": whole}


def _saved_amount(whole, currency):
    if not isinstance(whole, dict):
        return None
    if currency and whole.get("currency") != currency:
        return None
    return _money(whole.get("amount"))


def assess(text, context, whole):
    """Return a whole-trip turn, or None when the hotel-budget path should continue.

    Relative math uses only a previously saved whole-trip amount. The hotel
    request budget is never the base and is never replaced here.
    """
    from natural_language import amount_in_text, explicit_currency_in_text
    from refinement import whole_trip_budget_in

    if hotel_budget_phrase(text):
        return None
    route = bool(re.search(r"маршрут", text, re.I))
    other_edit = bool(
        re.search(r"перенес|сдвин|измени\s+дат|завтрак|реб[её]н|детск", text, re.I)
    )
    scope_edit = (
        context is not None
        and not other_edit
        and whole_trip_budget_in(text)
    )
    if not route and not scope_edit:
        return None

    explicit = explicit_currency_in_text(text)
    session_currency = getattr(context, "currency", None) if context is not None else None
    codes = {code.upper() for code in re.findall(r"\b[A-Za-z]{3}\b", text)}
    currency_blocked = bool(
        context is not None
        and (
            (explicit and explicit != session_currency)
            or (codes and codes != {session_currency})
        )
    )
    currency = session_currency or explicit
    base_amount = _saved_amount(whole, currency)
    percent = _percent(text)
    relative = needs_saved_base(text)
    amount = None
    if currency_blocked:
        amount = None
    elif relative:
        if base_amount is not None and currency:
            resolved = resolve_budget(text, base_amount, currency=currency)
            if resolved == "CURRENCY_MISMATCH":
                currency_blocked = True
            calculated = None
            if percent is not None and re.search(
                r"уменьш|сниз|сократ|уреж|сбав|опуст|скинь", text, re.I
            ):
                try:
                    calculated = apply_percent(base_amount, percent, currency)
                except ValueError:
                    calculated = None
            if not currency_blocked and calculated is not None and resolved in (None, calculated):
                amount = calculated
            elif not currency_blocked and isinstance(resolved, str):
                amount = _money(resolved)
    else:
        resolved = resolve_budget(text, None, currency=currency) if currency else resolve_budget(
            text, None, currency=None
        )
        if resolved == "CURRENCY_MISMATCH" and context is not None:
            currency_blocked = True
        elif isinstance(resolved, str):
            amount = _money(resolved)
        elif resolved is None:
            amount = _money(amount_in_text(text, scope="whole_trip"))
        if amount is not None and currency is None:
            amount = None
    if currency_blocked:
        amount = None

    limit = None
    if amount and currency and not currency_blocked:
        limit = {
            "amount": amount,
            "currency": currency,
            "scope": "whole_trip",
            "evidence": "USER_STATED",
        }
    info = assessment(limit)
    hotel_text = "не задан" if context is None else f"{context.budget} {context.currency}"
    same = (
        isinstance(whole, dict)
        and whole.get("amount") == amount
        and whole.get("currency") == currency
    )
    # An ordinary utterance never opens Preview and never writes either budget.
    # A separate whole-trip state starts only from confirm_separate_whole_trip.
    confirmation = None
    if amount and currency and context is not None and not same and not currency_blocked:
        confirmation = {
            "action": "confirm_separate_whole_trip",
            "confirmation": SEPARATE_WHOLE_TRIP_CONFIRMATION,
            "amount": amount,
            "currency": currency,
            "applied": False,
            "opens_preview": True,
        }
    if route and context is None:
        shown = f"{amount} {currency}" if amount and currency else "сумма или валюта не подтверждены"
        message = (
            "Весь маршрут не собран: невыполнимо подтвердить поездку без цен поставщика. "
            f"Лимит whole_trip, если он назван пользователем: {shown}. "
            "Отель, транспорт и трансфер: amount null, evidence UNKNOWN. "
            "Неизвестная цена не равна нулю. Наличие UNKNOWN. Сравнение NOT_PERFORMED. "
            "Цены, валюты и доступность не выдуманы. Ослабления без цен, метки MOCK и UNKNOWN: "
            + " ".join(item["text"] for item in RELAXATIONS)
        )
        confirmation = None
    elif context is not None and currency_blocked:
        message = (
            HOTEL_ONLY_CLARIFICATION
            + " Другая валюта не записана в бюджет отеля и не стала лимитом whole_trip. "
            "Цены маршрута UNKNOWN и не суммируются."
        )
    elif context is not None and confirmation and relative:
        message = (
            HOTEL_ONLY_CLARIFICATION
            + f" Отдельный расчёт whole_trip {base_amount} {currency} → {amount} {currency} "
            "не записан и Preview не открыт. Бюджет hotel_only "
            f"{hotel_text} не использован как база и не изменён. "
            "Цены отеля, транспорта и трансфера UNKNOWN. Сумма компонентов не собрана. "
            "Нужно явное действие confirm_separate_whole_trip."
        )
    elif context is not None and confirmation:
        message = (
            HOTEL_ONLY_CLARIFICATION
            + f" Отдельная сумма whole_trip {amount} {currency} не записана и Preview не открыт. "
            f"Бюджет hotel_only {hotel_text} не заменён. "
            "Цены отеля, транспорта и трансфера UNKNOWN и не суммируются. "
            "Нужно явное действие confirm_separate_whole_trip."
        )
    elif context is not None and relative:
        message = (
            HOTEL_ONLY_CLARIFICATION
            + f" Бюджет hotel_only {hotel_text} не изменён. "
            "Отдельный лимит whole_trip не задан, поэтому процент не применён "
            "и новая сумма не выдумана. Цены маршрута UNKNOWN."
        )
    elif context is not None:
        message = (
            HOTEL_ONLY_CLARIFICATION
            + f" Бюджет hotel_only {hotel_text} не изменён. "
            "Лимит whole_trip не обновлён: нужна явная сумма и валюта. Цены не выдуманы."
        )
    else:
        message = (
            "Лимит whole_trip не записан: нет сохранённой поездки. "
            "Цены компонентов UNKNOWN и не выдуманы."
        )
        confirmation = None
    return {
        "message": message,
        "assessment": info,
        "confirmation": confirmation,
        "spec": None,
    }


def render_scope_turn(app, sid, revision, turn):
    """Build the HTTP payload. Speech never writes a budget or opens Preview."""
    if sid:
        from session import Conflict

        with app.lock, app.store.transaction():
            _, session = app._load(sid)
            if type(revision) is not int or revision != session.state.revision:
                raise Conflict("STALE_REVISION")
            # New scope instructions supersede earlier unconfirmed offers.
            # The agreed hotel request and confirmed whole_trip remain untouched.
            app.store.invalidate(sid)
            saved = app.store.get_session(sid)
            if saved.pop("pending_budget_scopes", None) is not None:
                app.store.put_session(sid, saved)
            output = app.execute("read", sid=sid)
        output["has_preview"] = False
    else:
        output = {
            "session_id": None,
            "request": None,
            "revision": None,
            "cards": [],
            "has_preview": False,
            "source_mode": "UNKNOWN",
        }
    output["language_message"] = turn["message"]
    output["budget_assessment"] = turn["assessment"]
    output["whole_trip_confirmation"] = turn.get("confirmation")
    output["has_preview"] = False
    output["language"] = {
        "source": "LOCAL_SCOPE_RULES",
        "model_calls": 0,
        "validated_intent": {"intent": "clarify", "values": {}, "needs": []},
        "state_changed": False,
    }
    return output
