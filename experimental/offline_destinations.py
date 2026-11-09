"""Bounded, read-only destination brainstorming for the offline conversation."""

import re

from destination_proposals import propose_destinations


def destination_reply(text, current=None):
    # This is deliberately not a destination extractor. Any named/ambiguous
    # location or hotel request stays on the normal clarification/search path.
    if current is not None or re.search(r'[«»"\n]|отел|гостиниц|описани|источник', text, re.I):
        return None
    preferences = {}
    if re.search(r"т[её]пл\w*\s+мор|(?:к|на)\s+мор[ею]|warm sea", text, re.I):
        preferences["warm_sea"] = "unknown"
    if re.search(r"тих\w*|тишин|quiet", text, re.I):
        preferences["quiet"] = "not_applicable_at_destination_level"
    if re.search(
        r"коротк\w*\s+перел[её]т|без\s+долг\w*\s+(?:перел[её]т|переезд)|недолг\w*\s+лететь|short flight",
        text,
        re.I,
    ):
        preferences["short_flight"] = "unknown"
    if not preferences:
        return None
    # A conservative lexical allowlist makes the supported intent explicit and
    # prevents an unknown city from being replaced with a fixed demo candidate.
    words = re.findall(r"[а-яёa-z]+", text.casefold())
    allowed = re.compile(
        r"хочу|хотим|поездк\w*|поехать|отдых\w*|путешеств\w*|подбери|предложи|"
        r"направлен\w*|посоветуй|после|сесси\w*|недорого|куда|можно|полететь|мне|нам|нуж\w*|чтобы|было|и|а|но|с|"
        r"без|в|на|у|к|до|не|очень|т[её]пл\w*|мор\w*|тих\w*|тишин\w*|"
        r"коротк\w*|перел[её]т\w*|переезд\w*|бюджет|ограничен\w*|недолг\w*|долг\w*|лететь|want|a|trip|warm|sea|quiet|short|flight"
    )
    if any(allowed.fullmatch(word) is None for word in words) or re.search(
        r"[^А-Яа-яЁёA-Za-z\s,.:;!?()—–-]", text
    ):
        return None
    result = propose_destinations(preferences)
    for candidate in result["destinations"]:
        candidate["weather"] = None
    message = (
        "MOCK: демонстрационные направления: "
        + ", ".join(candidate["city"] for candidate in result["destinations"])
        + ". Цена, валюта, время перелёта и погода неизвестны. "
        "Соответствие пожеланиям не проверено; тишина зависит от конкретного жилья. "
        "Направление не выбрано, поездка не сохранена. Укажите город, даты, гостей "
        "и бюджет на отель с валютой для отдельного поиска жилья."
    )
    return {
        "session_id": None,
        "request": None,
        "revision": None,
        "cards": [],
        "has_preview": False,
        "destination_proposals": result,
        "source_mode": "MOCK",
        "language_message": message,
        "language": {
            "source": "LOCAL_DESTINATION_RULES",
            "model_calls": 0,
            "state_changed": False,
            "validated_intent": {"intent": "propose_destinations", "values": {}, "needs": []},
        },
    }


def stale_archive_prices(request, reference, cards):
    """Only hotel prices exist here; never invent transport or a trip total."""
    if (request.checkin, request.checkout) == (reference.checkin, reference.checkout):
        return {}
    for card in cards:
        card.update(
            price_status="STALE", recommendable=False, base_total=None, payable_for_method=None
        )
        if card["status"] != "REJECT":
            card["status"] = "REVIEW"
        card.setdefault("reasons", []).append("STALE_PRICE_CONTEXT")
        card.setdefault("reason_texts", []).append(
            "STALE: цена для новых дат неизвестна; нужен новый поиск."
        )
    return {
        "price_status": "STALE",
        "source_refresh_required": True,
        "invalidated_prices": ["hotel.amount"],
        "budget_scope": "hotel_only",
    }
