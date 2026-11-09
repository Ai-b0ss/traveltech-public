"""Field-specific user spans and explanations of already saved observations."""

import re
from decimal import Decimal, InvalidOperation
from datetime import date
from provider_transport import Failure


def single_date_span(quote):
    """One explicit calendar date; no inferred year/month or arbitrary digits."""
    iso = re.findall(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", quote)
    dots = re.findall(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)", quote)
    dates = [(int(y), int(m), int(d)) for y, m, d in iso] + [
        (int(y), int(m), int(d)) for d, m, y in dots
    ]
    from natural_language import M_RE, parse_month, YR_RE

    named = re.findall(rf"(?<!\d)(\d{{1,2}})\s+({M_RE})\s+(\d{{4}}){YR_RE}", quote, re.I)
    dates.extend((int(y), parse_month(m), int(d)) for d, m, y in named)
    if len(dates) != 1:
        return None
    try:
        return date(*dates[0]).isoformat()
    except ValueError:
        return None


def validate_spans(intent, evidence, text, current):
    from natural_language import (
        explicit_dates,
        amount_in_text,
        explicit_currency_in_text,
        local_rules,
    )
    from city_names import location_in_text, canonical_city
    from refinement import guests_in, breakfast_in
    from budget_math import resolve_budget

    if intent["intent"] not in ("search", "budget", "constraints", "refine", "preference"):
        return
    for field, value in intent["values"].items():
        quote = evidence.get(field, "")
        if not quote or quote not in text:
            raise Failure("SOURCE_SPAN_REQUIRED:" + field)
        if re.fullmatch(r"[A-Za-z0-9]+", quote.strip()) and not re.search(
            r"(?<!\w)" + re.escape(quote.strip()) + r"(?!\w)", text
        ):
            raise Failure("SOURCE_SPAN_DOES_NOT_SUPPORT_FIELD:" + field)
        observed = None
        if field in ("city", "country"):
            loc = location_in_text(quote)
            if loc is None:
                # An exact field quote can omit the preposition required by
                # whole-request discovery. Accept only the same explicit
                # location independently found in the full user request.
                whole = location_in_text(text)
                candidate = location_in_text("in " + quote)
                if whole and candidate and candidate[:2] == whole[:2]:
                    loc = candidate
                elif whole and field == "city" and quote.strip() == whole[0]:
                    loc = whole
            if loc:
                observed = loc[0 if field == "city" else 1]
            if field == "country" and re.fullmatch(r"[A-Z]{2}", quote.strip()):
                observed = quote.strip()
            if field == "city" and observed is not None:
                observed = canonical_city(
                    observed, intent["values"].get("country", getattr(current, "country", None))
                )[0]
        elif field in ("checkin", "checkout"):
            dates = explicit_dates(quote)
            observed = dates[0 if field == "checkin" else 1] if dates else single_date_span(quote)
            if observed is None and current is not None and intent["intent"] == "refine":
                from refinement import propose

                relative = propose(quote, current)
                if relative and relative["intent"] == "refine" and not relative["needs"]:
                    observed = relative["values"].get(field)
        elif field == "rooms":
            observed, needs = guests_in(
                quote,
                current if intent["intent"] == "refine" else None,
                require_rooms=intent["intent"] == "search",
            )
            if needs:
                observed = None
            # Defaults are admissible only for slots NOT explicitly stated.
            # A short quote cannot replace an explicit room count or child clause.
            from refinement import room_count_in
            if room_count_in(text) is not None and room_count_in(quote) is None:
                observed = None
            child_terms = r"реб[её]н|дет(?:ей|и)|без детей"
            if re.search(child_terms, text, re.I) and not re.search(child_terms, quote, re.I):
                observed = None
        elif field == "budget":
            observed = (
                resolve_budget(quote, current.budget, currency=current.currency)
                if current and intent["intent"] in ("budget", "refine")
                else amount_in_text(quote)
            )
            if observed is None and re.fullmatch(r"\d+(?:[.,]\d+)?", quote.strip()):
                observed = quote.strip().replace(",", ".")
            try:
                if observed not in (None, "INVALID", "CONFLICT") and Decimal(observed) == Decimal(
                    value
                ):
                    continue
            except (InvalidOperation, ValueError, TypeError):
                pass
        elif field == "currency":
            observed = explicit_currency_in_text(quote)
        elif field == "breakfast":
            observed = breakfast_in(quote)
        elif field in ("text", "evidence_status", "breakfast_wish"):
            observed = local_rules(quote, current).get("values", {}).get(field)
        if observed is None or type(observed) is not type(value) or observed != value:
            raise Failure("SOURCE_SPAN_DOES_NOT_SUPPORT_FIELD:" + field)


def saved_inspection(out, text, current):
    """No supplier/model calls. Every hotel reason retains its own saved card ID."""
    facts = {}
    rows = []

    def add(message, kind="POLICY", value=None, field=None, oid=None):
        refs = []
        if field:
            key = "saved:" + str(len(facts) + 1)
            facts[key] = {
                "field": field,
                "text": str(value),
                "source": "SAVED_TRIP",
                "offer_id": oid,
            }
            refs = [key]
        rows.append(
            {"text": message, "kind": kind, "fact_ids": refs, **({"offer_id": oid} if oid else {})}
        )

    cards = out.get("cards") or []
    if re.search(r"почему|отчего|причин|не подход", text, re.I):
        add("Сохранённые тарифы проверены на условия текущей поездки. Нового поиска не было.")
        for index, card in enumerate(cards[:24]):
            oid = card.get("offer_id") or "saved-card-" + str(index + 1)
            name = card.get("name", "Тариф")
            for reason in dict.fromkeys(
                card.get("reason_texts") or ["Наличие и окончательная цена не подтверждены"]
            ):
                add(name + ": " + reason + ".", "SAVED_OFFER_REASON", reason, "reason", oid)
        if len(cards) > 24:
            add("Показаны причины первых 24 сохранённых тарифов; остальные доступны в карточках.")
        if not cards:
            add("Сохранённых тарифов нет. Это не доказательство отсутствия гостиниц в городе.")
        if current:
            add(
                "Валюта бюджета: " + current.currency + ".",
                "REQUEST_STATE",
                current.currency,
                "request_currency",
            )
        currencies = sorted({c.get("currency") for c in cards if c.get("currency")})
        if currencies:
            add(
                "Валюты тарифов: " + ", ".join(currencies) + ".",
                "SAVED_STATE",
                ", ".join(currencies),
                "offer_currencies",
            )
    elif re.search(r"заезд|выезд|check.?in|check.?out", text, re.I):
        for index, card in enumerate(cards[:24]):
            d = card.get("hotel_detail_evidence") or {}
            oid = card.get("offer_id") or "saved-card-" + str(index + 1)
            for key, label in [("checkin_text", "Заезд"), ("checkout_text", "Выезд")]:
                if d.get(key):
                    add(
                        card.get("name", "Отель") + " · " + label + ": " + d[key] + ".",
                        "SAVED_HOTEL_DETAIL",
                        d[key],
                        key,
                        oid,
                    )
        if not rows:
            add("Правила заезда и выезда в сохранённых данных не получены.")
        add(
            "Это правила отеля из сохранённого источника; условия конкретного номера требуют подтверждения."
        )
    else:
        add(
            "Условия отмены требуют подтверждения по конкретному тарифу; локальная отмена поездки не выполнялась."
            if re.search(r"отмен", text, re.I)
            else "Неподтверждённые условия сохранены. Налоги, вместимость и наличие требуют соответствующего источника."
        )
    return {"text": " ".join(r["text"] for r in rows), "statements": rows, "facts": facts}
