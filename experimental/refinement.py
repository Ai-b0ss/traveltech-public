"""Explicit conversational refinements, independently reproducible from user text."""

import re
from dataclasses import replace
from travel_core import Guests, InputError
from budget_math import resolve_budget
from city_names import location_in_text


def whole_trip_budget_in(text):
    trip_scope = bool(re.search(
        r"бюджет\s+(?:(?:на|для)\s+)?(?:(?:вс[юей]+|весь|всего|нашей|этой)\s+)?"
        r"(?:поездк[уи]|путешестви[ея]|маршрут[а]?)",
        text, re.I,
    ))
    hotel_scope = bool(re.search(
        r"бюджет\s+(?:только\s+)?(?:на|для)\s+(?:отел\w*|гостиниц\w*|проживани\w*)",
        text, re.I,
    ))
    return trip_scope or (not hotel_scope and bool(re.search(r"общ(?:ий|его|ему)\s+бюджет", text, re.I)))


def is_undo(text):
    return bool(
        re.fullmatch(
            r"\s*(?:отмени(?:\s+(?:последн\w*\s+)?(?:правк\w*|изменени\w*|действи\w*|операци\w*))?|откати(?:\s+последн\w*\s+(?:правк\w*|изменени\w*))?|верни\s+(?:прежн\w*|предыдущ\w*)\s+(?:услови\w*|состояни\w*|верси\w*|бюджет))\s*[.!?]*\s*",
            text,
            re.I,
        )
    )


def breakfast_in(text):
    if re.search(
        r"желательно|пожелание|не\s*обязательн\w*\s+(?:с\s+)?завтрак|завтрак\s+не\s*обязател",
        text,
        re.I,
    ):
        return None
    positive_text = re.sub(
        r"не\s+нужен\s+завтрак|завтрак\s+не\s*обязател[а-я]*|не\s*обязательн\w*\s+завтрак",
        "",
        text,
        flags=re.I,
    )
    positive = bool(
        re.search(
            r"нужен\s+завтрак|завтрак\s+(?:должен\s+быть\s+|был\s+)?(?:обязател|включ[её]н)|(?:сделай|добавь|включи|только).*завтрак|(?<!не)(?<!не\s)обязательн\w*\s+(?:с\s+)?завтрак|с\s+завтраком\s+обязательно|включ[её]н\w*\s+завтрак|(?<!не\s)(?<!без\s)\bс\s+завтраком\b",
            positive_text,
            re.I,
        )
    )
    negative = bool(
        re.search(
            r"без\s+завтрака|завтрак\s+(?:не\s*нужен|не\s*обязател)|(?:убери|исключи).*завтрак|не\s+нужен\s+завтрак|не\s*обязательн\w*\s+завтрак",
            text,
            re.I,
        )
    )
    if positive and negative:
        return "CONFLICT"
    return False if negative else True if positive else None


ADULT_NUMBER_WORDS = (
    (1, r"\b(?:один|одного|одному)\s+(?:взросл\w*|гост(?:ь|я|ю|ей|и)|человек\w*)"),
    (2, r"\b(?:двое|два|двух|двум)\s+(?:взросл\w*|гост(?:я|ей|и)|человек\w*)"),
    (3, r"\b(?:трое|троих|три|трёх|трех)\s+(?:взросл\w*|гост(?:я|ей|и)|человек\w*)"),
    (4, r"\b(?:четверо|четверых|четыре|четырёх|четырех)\s+(?:взросл\w*|гост(?:я|ей|и)|человек\w*)"),
)


def adult_count_in(text):
    """Read the number of adult travelers, not the number of booked rooms.

    Explicit conflicts are preserved as CONFLICT and require clarification;
    the one-room default is considered later, only when room count is absent.
    """
    # Never silently choose one count if the user gives conflicting numbers.
    found = {
        int(m.group(1))
        for m in re.finditer(r"\b(\d+)\s+(?:взросл\w*|человек\w*|чел\b|гост\w*)", text, re.I)
    }
    for amount, pattern in ADULT_NUMBER_WORDS:
        if re.search(pattern, text, re.I):
            found.add(amount)
    if re.search(r"\b(?:в\s+)?соло\b|\bв\s+одиночку\b", text, re.I):
        found.add(1)
    # Only a complete clause denotes a solo traveller; "один номер" or
    # "один из отелей" must never become guest evidence.
    if re.search(
        r"(?:^|[,;])\s*один\s*(?=[,;.!]|$)|\b(?:еду|поеду|путешествую)\s+один\b", text, re.I
    ):
        found.add(1)
    if re.search(r"\b(?:я|еду|поеду|путешествую|буду|остановлюсь)\s+один\b", text, re.I):
        found.add(1)
    # "Хочу поехать в Берлин 22-24 октября один" is one traveller.
    # Keep the trip-verb context and a clause boundary so "один номер",
    # "один из отелей" and unrelated ordinal mentions are not guests.
    if re.search(
        r"\b(?:поехать|ехать|полететь|отправиться|путешествовать|"
        r"путешествую|еду|поеду|лечу|отдыхаю|отдыхать)\b"
        r"[^.!?\n]{0,160}?\b(?:один|одна|одному|одной)\b(?=\s*(?:[,;.!?]|$))",
        text, re.I,
    ):
        found.add(1)
    # A traveller count may precede an explicit date:
    # "Хочу поехать в Берн один 24–26 октября".
    if re.search(
        r"\b(?:поехать|еду|поеду|полететь|лечу|отправиться|путешествовать)\b"
        r"[^.!?\n]{0,150}?\b(?:один|одна)\b(?=\s+(?:\d{1,2}\s*(?:[-–]|[а-яё])|бюджет\b|есть\b))",
        text, re.I,
    ):
        found.add(1)
    if re.search(r"\bна\s+двоих\b|\bвдво[её]м\b", text, re.I):
        found.add(2)
    if len(found) > 1:
        return "CONFLICT"
    return next(iter(found)) if found else None


ROOM_NUMBER_WORDS = (
    (
        1,
        r"\b(?:\u043e\u0434\u0438\u043d|\u043e\u0434\u043d\u043e\u0433\u043e|\u043e\u0434\u043d\u043e\u043c\u0443)\s+\u043d\u043e\u043c\u0435\u0440\w*",
    ),
    (2, r"\b(?:\u0434\u0432\u0430|\u0434\u0432\u0443\u0445)\s+\u043d\u043e\u043c\u0435\u0440\w*"),
    (
        3,
        r"\b(?:\u0442\u0440\u0438|\u0442\u0440\u0451\u0445|\u0442\u0440\u0435\u0445)\s+\u043d\u043e\u043c\u0435\u0440\w*",
    ),
    (
        4,
        r"\b(?:\u0447\u0435\u0442\u044b\u0440\u0435|\u0447\u0435\u0442\u044b\u0440\u0451\u0445|\u0447\u0435\u0442\u044b\u0440\u0435\u0445)\s+\u043d\u043e\u043c\u0435\u0440\w*",
    ),
)


def room_count_in(text):
    found = {
        int(m.group(1))
        for m in re.finditer(r"\b(\d+)\s+\u043d\u043e\u043c\u0435\u0440\w*", text, re.I)
    }
    for amount, pattern in ROOM_NUMBER_WORDS:
        if re.search(pattern, text, re.I):
            found.add(amount)
    if len(found) > 1:
        return "CONFLICT"
    return next(iter(found)) if found else None


ADDITIVE_GUEST = re.compile(
    r"\b(?:добавь|добавьте|добавить)\b[^.;!?]{0,60}?(?:реб[её]н|дет(?:ей|и)|взросл)|"
    r"\bещ[её]\s+(?:(?:одного|один|одна|два|двух|\d+)\s+)?(?:реб[её]н|взросл)", re.I
)


def additive_guests_in(text, context):
    """Append only explicit new guests to an unambiguous existing room.

    Request limits are checked by normalize/Request.validate; supplier capacity
    remains an offer constraint, never permission to book or split the party.
    """
    if re.search(r"\b(?:не|или|либо|вместо|замени|заменить|убери|теперь)\b|без детей", text, re.I):
        return None, ["guests_conflict"]
    # A room ordinal targets an existing allocation; a new room count is a
    # different operation and must not silently redistribute current guests.
    target = re.findall(r"\bв\s+номер\s+(\d+)\b", text, re.I)
    room_count = room_count_in(text)
    same_single_room = len(context.rooms) == 1 and room_count == 1
    if len(target) > 1 or (room_count is not None and not same_single_room):
        return None, ["rooms"]
    if target:
        index = int(target[0]) - 1
        if not 0 <= index < len(context.rooms):
            return None, ["rooms"]
    elif len(context.rooms) == 1:
        index = 0
    else:
        return None, ["rooms"]
    # Account for every room mention, including an unsupported second target.
    allocation_rest = re.sub(r"\bв\s+номер\s+\d+\b", "", text, flags=re.I)
    if same_single_room:
        allocation_rest = re.sub(
            r"\bв\s+(?:1|один)\s+номер\b", "", allocation_rest, flags=re.I
        )
    if re.search(r"\bномер\w*\b", allocation_rest, re.I):
        return None, ["rooms"]
    adults = adult_count_in(text)
    if adults == "CONFLICT":
        return None, ["guests_conflict"]
    child = bool(re.search(r"реб[её]н|дет(?:ей|и)", text, re.I))
    # Mixed clauses can mean either a delta or a new total. Ask rather than
    # interpret the other count as additive without its own confirmed intent.
    if child and adults is not None:
        return None, ["guests_conflict"]
    rooms = [{"adults": r.adults, "children": list(r.children)} for r in context.rooms]
    if child:
        if re.search(r"\bдет(?:ей|и)\b", text, re.I) and not re.search(
            r"\b(?:добавь|добавьте|добавить|ещ[её])\s+"
            r"(?:(?:\d+|двое|двоих|два|двух|трое|троих|три|трех|трёх|"
            r"четверо|четверых|четыре|четырех|четырёх)\s+)?дет(?:ей|и)\b", text, re.I
        ):
            return None, ["child_age"]
        # Reuse age/count parsing with an empty child list, without recursion.
        from types import SimpleNamespace
        empty = SimpleNamespace(rooms=(Guests(rooms[index]["adults"]),))
        added, needs = guests_in(text, empty, _allow_additive=False)
        if needs:
            return None, needs
        rooms[index]["children"].extend(added[0]["children"])
    elif adults is not None:
        # Equal counts in separate clauses are still separate people. Do not
        # collapse them through adult_count_in's conflict-detection set.
        if len(re.findall(r"\bвзросл\w*", text, re.I)) != 1:
            return None, ["guests_conflict"]
        rooms[index]["adults"] += adults
    else:
        return None, ["guests_conflict"]
    return rooms, []


def guests_in(text, context=None, require_rooms=False, _allow_additive=True):
    if context and _allow_additive and ADDITIVE_GUEST.search(text):
        return additive_guests_in(text, context)
    adult_count = adult_count_in(text)
    if adult_count == "CONFLICT":
        return None, ["guests_conflict"]
    rooms = room_count_in(text)
    if rooms == "CONFLICT":
        return None, ["rooms"]
    child_mentioned = bool(re.search(r"реб[её]н|дет(?:ей|и)|без детей", text, re.I))
    if adult_count is None and not child_mentioned and rooms is None:
        return None, []
    if rooms is not None and rooms != 1 or context and len(context.rooms) != 1:
        return None, ["rooms"]
    # One solo adult normally needs one room; expose this as a visible,
    # reversible default in Preview, never a hidden booking instruction.
    if require_rooms and rooms is None and adult_count in (1, 2) and not child_mentioned:
        rooms = 1
    if require_rooms and rooms is None:
        return None, ["rooms"]
    current = context.rooms[0] if context else None
    if adult_count is None:
        adult_count = current.adults if current else None
    if adult_count is None:
        return None, ["rooms"]
    children = list(current.children) if current else []
    if child_mentioned:
        # With existing children, a bare age is neither an explicit addition
        # nor an explicit replacement. Do not silently lose confirmed people.
        if (
            current and current.children and _allow_additive
            and not re.search(
                r"без детей|убери|\b(?:теперь\s+едем|замени|заменить)\b", text, re.I
            )
        ):
            return None, ["guests_conflict"]
        if re.search(r"без детей|убери.*реб[её]н", text, re.I):
            children = []
        else:
            cnt_m = re.search(
                r"\b(\d+|двое|двоих|два|двух|трое|троих|три|трех|трёх|четверо|четверых|четыре|четырех|четырёх)\s+(?:детей|реб[её]нк\w*|дет\w*)",
                text,
                re.I,
            )
            c_map = {
                "2": 2,
                "двое": 2,
                "двоих": 2,
                "два": 2,
                "двух": 2,
                "3": 3,
                "трое": 3,
                "троих": 3,
                "три": 3,
                "трех": 3,
                "трёх": 3,
                "4": 4,
                "четверо": 4,
                "четверых": 4,
                "четыре": 4,
                "четырех": 4,
                "четырёх": 4,
            }
            count = cnt_m.group(1).lower() if cnt_m else "1"
            child_mult = int(count) if count.isdigit() else c_map[count]
            if not 1 <= child_mult <= 16:
                return None, ["child_age"]
            # A fresh one-room search with more than four children is not a
            # validated family allocation; request clarification and explicit
            # rooms instead of assuming a feasible party distribution.
            if context is None and child_mult > 4:
                return None, ["rooms"]
            singles = re.findall(
                r"реб[её]н(?:ок|ка|ку|ком|ке)?\s*(?:в возрасте\s*|возраст\s*|по\s*)?(\d{1,2})\s*(?:лет|год(?:а|ов)?)\b",
                text,
                re.I,
            )
            plurals = re.findall(
                r"(?:дети|детей)\s+(?:по\s+)?(\d{1,2}(?:\s*(?:,|и)\s*\d{1,2})*)\s*(?:лет|год(?:а|ов)?)\b", text, re.I
            )
            if (singles and plurals) or len(plurals) > 1:
                return None, ["child_age"]
            if singles:
                raw_ages = [int(age) for age in singles]
            elif plurals:
                raw_ages = [int(x) for x in re.findall(r"\d+", plurals[0])]
            else:
                return None, ["child_age"]
            if child_mult > 1 and len(raw_ages) not in (1, child_mult):
                return None, ["child_age"]
            if (child_mult > 1 and len(raw_ages) == 1
                    and ADDITIVE_GUEST.search(text)
                    and not re.search(
                        r"\b(?:дети|детей|реб[её]н(?:ок|ка|ку|ком|ке)?)\s+по\s+\d+", text, re.I
                    )):
                return None, ["child_age"]
            if re.search(r"\bили\b|\bлибо\b|\bне\b", text, re.I):
                return None, ["guests_conflict"]
            if len(re.findall(r"\b\d{1,2}\s*(?:лет|год[а-я]*)", text, re.I)) > len(raw_ages):
                return None, ["child_age"]
            # Every child clause needs ages, even if a prior plural clause
            # already supplies several ages. Equal ages stay separate entries.
            mentions = re.findall(r"\b(?:реб[её]н\w*|дет(?:ей|и))\b", text, re.I)
            if len(mentions) != len(singles) + len(plurals):
                return None, ["child_age"]
            if re.search(
                r"(?:реб[её]н(?:ок|ка|ку|ком|ке)?|дети|детей)\s*"
                r"(?:в возрасте\s*|возраст\s*|по\s*)?\d{3,}\s*(?:лет|год)", text, re.I
            ):
                return None, ["child_age"]
            if len(raw_ages) == 1 and child_mult > 1:
                children = raw_ages * child_mult
            else:
                children = raw_ages
    return [{"adults": adult_count, "children": children}], []


def propose(text, context):
    if context is None or re.search(r"подбери|найди|ищи", text, re.I):
        return None
    if re.search(r"отмени.*бронирован|забронируй|оплати", text, re.I):
        return {"intent": "inspect", "values": {}, "needs": ["availability"]}
    if re.search(
        r"(?:почему|отчего|из-за чего).*?(?:нет|не найден|не наш[её]л).*?(?:отел|тариф|вариант|предложени)",
        text,
        re.I,
    ):
        return {"intent": "inspect", "values": {}, "needs": ["request"]}
    if re.search(
        r"(?:почему|отчего|из-за чего).*?(?:тариф|вариант|предложени|отел).*?(?:не подходит|не подходят|отклон|не годятся|нет подходящ)",
        text,
        re.I,
    ):
        return {"intent": "inspect", "values": {}, "needs": ["request"]}
    if re.search(
        r"во\s+сколько\s+(?:заезд|выезд)|время\s+(?:заезда|выезда)|какой\s+час\s+(?:заезда|выезда)|правил[а-я]*\s+заезда",
        text,
        re.I,
    ):
        return {"intent": "inspect", "values": {}, "needs": ["request"]}
    if re.search(
        r"(?:покажи|объясни|расскажи|какие|каковы).*отмен|условия отмен|правила отмен", text, re.I
    ):
        return {"intent": "inspect", "values": {}, "needs": ["request"]}
    if is_undo(text):
        return {"intent": "undo", "values": {}, "needs": []}
    patch = {}
    needs = []
    budget_requested = bool(re.search(r"бюджет", text, re.I))
    if budget_requested:
        # A Request is a hotel request, not a personal whole-trip TripPlan.
        if whole_trip_budget_in(text):
            return {"intent": "clarify", "values": {}, "needs": ["budget"]}
        from natural_language import explicit_currency_in_text

        currency = explicit_currency_in_text(text)
        codes = {code.upper() for code in re.findall(r"\b[A-Za-z]{3}\b", text)}
        if codes and codes != {context.currency}:
            raise InputError("BUDGET_CURRENCY_CHANGE_REQUIRES_NEW_REQUEST")
        if currency and currency != context.currency:
            raise InputError("BUDGET_CURRENCY_CHANGE_REQUIRES_NEW_REQUEST")
        result = resolve_budget(text, context.budget, currency=context.currency)
        if result == "CONFLICT":
            return {"intent": "clarify", "values": {}, "needs": ["budget_conflict"]}
        if result in ("INVALID", None):
            needs.append("budget")
        elif result != context.budget:
            patch["budget"] = result
    breakfast = breakfast_in(text)
    if breakfast == "CONFLICT":
        needs.append("request")
    elif breakfast is not None:
        patch["breakfast"] = breakfast
    is_time_inquiry = bool(re.search(r"во\s+сколько|время\s+(?:заезда|выезда)", text, re.I))
    date_requested = bool(
        not is_time_inquiry
        and re.search(r"перенеси|измени\s+дат|сдвинь|заезд|выезд|даты\s+(?:на|с)", text, re.I)
    )
    if date_requested:
        from natural_language import explicit_dates

        dates = explicit_dates(text)
        if dates:
            patch.update(checkin=dates[0], checkout=dates[1])
        elif (relative := re.search(
            r"(?:сдвинь|перенеси)\s+(?:существующ\w*\s+|текущ\w*\s+)?(?:даты|поездку)\s+на\s+(\d{1,3}|два|двое)\s+"
            r"(?:дня|дней|день)\s+(позже|раньше)", text, re.I
        )):
            from destination_proposals import shift_dates

            raw_days = relative[1].casefold()
            days = 2 if raw_days in ("два", "двое") else int(raw_days)
            days *= -1 if relative[2].casefold() == "раньше" else 1
            try:
                if len(re.findall(r"на\s+(?:\d+|два|двое)\s+(?:дня|дней|день)", text, re.I)) != 1 or re.search(r"\bили\b|\bлибо\b", text, re.I):
                    raise ValueError("AMBIGUOUS_DATE_SHIFT")
                shifted = shift_dates({"checkin": context.checkin, "checkout": context.checkout}, days)
                patch.update(checkin=shifted["checkin"], checkout=shifted["checkout"])
            except ValueError:
                needs += ["checkin", "checkout"]
        elif re.search(r"\d|даты|заезд|выезд", text, re.I):
            needs += ["checkin", "checkout"]
    guest_requested = bool(
        ADDITIVE_GUEST.search(text)
        or adult_count_in(text) is not None
        or room_count_in(text) is not None
        or re.search(
            r"\d+\s+(?:взросл|номер)|один номер|(?:добавь|едем|теперь).*?(?:реб[её]н|дет)|без детей|реб[её]н(?:ок|ка)?\s+\d+|дети\s+\d+|(?:двое|трое|четверо|два|три|четыре)\s+(?:детей|реб[её]нк\w*)",
            text,
            re.I,
        )
    )
    rooms, guest_needs = guests_in(text, context) if guest_requested else (None, [])
    if rooms is not None:
        patch["rooms"] = rooms
    needs += guest_needs
    if re.search(
        r"(?:перенеси|поменяй|смени|измен[иить]+).*?(?:город|в\s+(?:Сочи|Дубай|Dubai|Istanbul|Стамбул))",
        text,
        re.I,
    ):
        location = location_in_text(text)
        if location:
            patch.update(city=location[0], country=location[1])
        else:
            needs.append("city")
    # Never silently apply just the recognized half of a compound command.
    if re.search(r"(?:и|но|также)\s+(?:поменяй|измени|перенеси|смени)", text, re.I) and not (
        date_requested or "city" in patch or "rooms" in patch
    ):
        needs.append("request")
    if budget_requested and re.search(r"желательно.*завтрак", text, re.I):
        needs.append("request")
    if needs:
        preserved = {}
        if context:
            preserved = {
                "city": context.city,
                "country": context.country,
                "rooms": [
                    {"adults": r.adults, "children": list(r.children)} for r in context.rooms
                ],
                "budget": context.budget,
                "currency": context.currency,
            }
            if context.breakfast is not None:
                preserved["breakfast"] = context.breakfast
        preserved.update(patch)
        res = {"intent": "clarify", "values": preserved, "needs": list(dict.fromkeys(needs))}
        if date_requested and not dates:
            from natural_language import detect_date_error

            date_err = detect_date_error(text)
            if date_err:
                res["date_error"] = date_err
                if "invalid_dates" not in res["needs"]:
                    res["needs"].append("invalid_dates")
        return res
    if not patch:
        return None
    kind = (
        "budget"
        if set(patch) == {"budget"}
        else "constraints"
        if set(patch) == {"breakfast"}
        else "refine"
    )
    try:
        normalize(context, patch).validate()
    except InputError:
        return {
            "intent": "clarify",
            "values": {},
            "needs": ["rooms"] if "rooms" in patch else ["request"],
        }
    return {"intent": kind, "values": patch, "needs": []}


def normalize(context, patch):
    patch = dict(patch)
    if "rooms" in patch:
        if not isinstance(patch["rooms"], list) or any(
            not isinstance(x, dict) or set(x) != {"adults", "children"} for x in patch["rooms"]
        ):
            raise InputError("LANGUAGE_ROOMS_INVALID")
        patch["rooms"] = tuple(Guests(x["adults"], tuple(x["children"])) for x in patch["rooms"])
    return replace(context, **patch)
