"""Durable deterministic slot accumulation for a new-trip draft on the local and model paths.

Only attributes explicitly provided by a user enter this draft. Missing child ages
or ambiguous room allocation cannot be silently inferred or committed.
"""

import json
import re
from types import SimpleNamespace


def _children_in(text, needs_before):
    from refinement import guests_in
    from travel_core import Guests
    from pet_preferences import guest_text

    text = guest_text(text)

    child_mentioned = bool(re.search(r"реб[её]н|дет(?:ей|и)|без детей", text, re.I))
    if not child_mentioned and not needs_before:
        return None
    if not child_mentioned:
        # A reply such as "8 лет" is an age ONLY after we explicitly asked
        # the user for a missing child's age.
        age = re.fullmatch(
            r"\s*(\d{1,2}(?:\s*(?:,|и)\s*\d{1,2})*)\s*(?:лет|год(?:а|ов)?)?\s*[.!]?\s*", text, re.I
        )
        return [int(a) for a in re.findall(r"\d+", age[1])] if age else None
    context = SimpleNamespace(rooms=(Guests(2),))
    result, needs = guests_in(text, context)
    if "child_age" in needs:
        return "MISSING"
    if result is None:
        return "MISSING"
    return result[0]["children"]


def prepare_local(app, text, draft_id):
    from natural_language import (
        local_rules, REQUIRED, source_quote_inspection, explicit_currency_in_text,
    )
    from refinement import adult_count_in, room_count_in
    from scoped_budget import initial_scopes

    if not draft_id or source_quote_inspection(text) is not None:
        return text
    if not isinstance(draft_id, str) or not re.fullmatch("[a-f0-9]{32}", draft_id):
        return text
    raw = local_rules(text, None)
    scoped = initial_scopes(text)
    if raw["intent"] not in ("search", "clarify"):
        return text

    adult = adult_count_in(text)
    room_count = room_count_in(text)
    conflicts = set(raw["needs"]) & {"budget_conflict", "guests_conflict"}
    if adult == "CONFLICT" or room_count == "CONFLICT":
        conflicts.add("guests_conflict")

    with app.lock, app.store.transaction():
        app.store.db.execute(
            "CREATE TABLE IF NOT EXISTS local_draft_inputs(id TEXT PRIMARY KEY,body TEXT NOT NULL)"
        )
        row = app.store.db.execute(
            "SELECT body FROM local_draft_inputs WHERE id=?", (draft_id,)
        ).fetchone()
        saved = (
            json.loads(row[0])
            if row
            else {"values": {}, "invalid_text": None, "guest_parts": {}, "child_age_pending": False}
        )
        if saved.get("city_candidate") and re.fullmatch(
            r"\s*(?:да|верно|правильно|подтверждаю)[.!]?\s*", text, re.I
        ):
            raw = local_rules("в " + saved["city_candidate"], None)
        if raw["intent"] == "search":
            from pet_preferences import parse as pet_parse

            # Completing an earlier draft must retain its material pet clause.
            # An explicit replacement in this turn supersedes that clause.
            pet_wishes = [p for p in saved.get("hotel_preferences", []) if pet_parse(p)]
            saved = {
                "values": {},
                "invalid_text": None,
                "guest_parts": {},
                "child_age_pending": False,
                "hotel_preferences": pet_wishes if pet_parse(text) is None else [],
            }
        values = saved.setdefault("values", {})
        scopes = saved.get("pending_budget_scopes")
        if scoped["active"] or scoped.get("hotel_only") or scopes:
            scopes = saved.setdefault("pending_budget_scopes", {})
            # A plain 'budget 30000' after a whole-trip question cannot silently
            # become a hotel allocation. Ask for an explicit scope instead.
            ambiguous = bool(scopes.get("whole_trip") and raw["values"].get("budget")
                             and not scoped.get("hotel_only") and not scoped.get("whole_trip"))
            if scoped["conflict"] or ambiguous:
                scopes["conflict"] = True
                scopes["conflict_text"] = text
                if ambiguous:
                    scopes.pop("hotel_only", None)
                values.pop("budget", None)
                raw["values"].pop("budget", None)
                raw["values"].pop("currency", None)
                conflicts.add("budget_conflict")
            else:
                if not scopes.get("whole_trip") and raw["values"].get("budget"):
                    # Preserve the existing generic hotel-budget correction path
                    # when no whole-trip amount has ever been stated.
                    scopes.pop("conflict", None)
                    scopes.pop("conflict_text", None)
                for scope in ("whole_trip", "hotel_only"):
                    if scoped.get(scope):
                        scopes[scope] = scoped[scope]
                if (scoped["has_whole"] and not scoped.get("whole_trip")
                        and scopes.get("whole_trip") and explicit_currency_in_text(text)):
                    scopes["whole_trip"]["currency"] = explicit_currency_in_text(text)
                if scoped.get("hotel_only") or (
                    scoped.get("whole_trip") and not scopes.get("hotel_only")
                ):
                    scopes.pop("conflict", None)
                    scopes.pop("conflict_text", None)
                if scoped.get("whole_trip") and not scoped.get("hotel_only"):
                    # Earlier hotel values cannot stand in for a newly stated total.
                    # Explicit hotel evidence, if collected earlier, remains separate.
                    if not scopes.get("hotel_only"):
                        values.pop("budget", None)
                        values.pop("currency", None)
        fragments = saved.setdefault("guest_parts", {})
        old_rooms = values.pop("rooms", None)
        if isinstance(old_rooms, list) and len(old_rooms) == 1 and isinstance(old_rooms[0], dict):
            room = old_rooms[0]
            if type(room.get("adults")) is int and isinstance(room.get("children"), list):
                fragments.setdefault("adults", room["adults"])
                fragments.setdefault("room_count", 1)
                fragments.setdefault("children", room["children"])
        unresolved = saved.setdefault("conflicts", {})
        pending = bool(saved.get("child_age_pending"))
        child = _children_in(text, pending)
        hotel_preferences = preference_fragments(text)
        meaningful = bool(
            hotel_preferences
            or raw["values"]
            or raw.get("date_error")
            or adult is not None
            or room_count is not None
            or child is not None
            or conflicts
            or raw.get("city_candidate")
            or scoped["active"]
            or scopes
        )
        if not meaningful:
            return text
        # A new draft instruction makes the PREVIOUS offer unsafe to confirm,
        # even when the new instruction is still incomplete.
        app.store.db.execute(
            "UPDATE proposals SET lifecycle='SUPERSEDED' "
            "WHERE id IN (SELECT preview_id FROM draft_groups WHERE id=?) "
            "AND lifecycle='PENDING'",
            (draft_id,),
        )

        incoming = {
            k: v
            for k, v in raw["values"].items()
            if k in REQUIRED | {"breakfast", "breakfast_wish"}
        }
        # An entire allocation supplied explicitly in a single message
        # counts as evidence. A partial allocation is kept as fragments.
        if "rooms" in incoming:
            fully_parsed = incoming.pop("rooms")
            if len(fully_parsed) == 1 and set(fully_parsed[0]) == {"adults", "children"}:
                fragments["adults"] = fully_parsed[0]["adults"]
                fragments["room_count"] = 1
                fragments["children"] = fully_parsed[0]["children"]
                saved["child_age_pending"] = False
            else:
                fragments["room_count"] = len(fully_parsed)
        if adult is not None and adult != "CONFLICT":
            fragments["adults"] = adult
        if room_count is not None and room_count != "CONFLICT":
            fragments["room_count"] = room_count
        # Conflicting edits invalidate the affected slot until explicitly corrected.
        # Keeping the original clause lets the normal validation reject guesses.
        if "budget" in incoming:
            unresolved.pop("budget_conflict", None)
        if adult is not None and adult != "CONFLICT" and room_count != "CONFLICT":
            unresolved.pop("guests_conflict", None)
        if "budget_conflict" in conflicts:
            values.pop("budget", None)
            unresolved["budget_conflict"] = text
        if "guests_conflict" in conflicts:
            fragments.pop("adults", None)
            if room_count == "CONFLICT":
                fragments.pop("room_count", None)
            unresolved["guests_conflict"] = text
        count_match = re.search(
            r"\b(\d+|два|двое|двух|двоих|три|трое|трех|трёх|троих|четыре|четверо|четырех|четырёх|четверых)\s+(?:детей|дети|реб[её]нк\w*)",
            text,
            re.I,
        )
        counts = {
            "2": 2,
            "два": 2,
            "двое": 2,
            "двух": 2,
            "двоих": 2,
            "3": 3,
            "три": 3,
            "трое": 3,
            "трех": 3,
            "трёх": 3,
            "троих": 3,
            "4": 4,
            "четыре": 4,
            "четверо": 4,
            "четырех": 4,
            "четырёх": 4,
            "четверых": 4,
        }
        if count_match:
            token = count_match[1].lower()
            saved["child_count"] = int(token) if token.isdigit() else counts[token]
            saved["partial_child_ages"] = []
        elif re.search(r"с ребёнком|с ребенком|реб[её]нок", text, re.I) and not pending:
            saved["child_count"] = 1
        if re.search(r"без детей|убери.*реб[её]н", text, re.I):
            saved["child_count"] = 0
            saved["partial_child_ages"] = []
            pending = False
        if isinstance(child, list) and (pending or count_match):
            expected = saved.get("child_count", 1)
            partial = saved.get("partial_child_ages", [])
            ages = child if count_match else partial + child
            if len(ages) != expected or any(not 0 <= a <= 17 for a in ages):
                saved["partial_child_ages"] = (
                    ages if len(ages) < expected and all(0 <= a <= 17 for a in ages) else []
                )
                child = "MISSING"
            else:
                child = ages
                saved["partial_child_ages"] = []
        # Five (or more) declared children with only a single age mention
        # is incomplete: never infer all ages from a shorthand.  Retain the
        # first explicit age and ask for the remaining ages.
        shorthand = re.search(
            r"\b\d+\s+детей\s+(\d{1,2})\s+лет\b", text, re.I
        )
        if count_match and saved.get("child_count", 0) > 4 and shorthand:
            saved["partial_child_ages"] = [int(shorthand.group(1))]
            child = "MISSING"
        if child == "MISSING":
            # Unknown age deliberately invalidates prior child allocation.
            fragments.pop("children", None)
            saved["child_age_pending"] = True
        elif child is not None:
            fragments["children"] = child
            saved["child_age_pending"] = False

        from pet_preferences import parse as pet_parse

        if pet_parse(text) in ("required", "absent"):
            saved["hotel_preferences"] = [
                p for p in saved.get("hotel_preferences", []) if pet_parse(p) is None
            ]
        for item in hotel_preferences:
            kept = saved.setdefault("hotel_preferences", [])
            if item not in kept:
                kept.append(item)
        values.update(incoming)
        if incoming.get("city"):
            saved.pop("city_candidate", None)
            saved.pop("location_text", None)
        elif raw.get("city_candidate"):
            from city_names import city_typo_candidate

            candidate = city_typo_candidate(text)
            saved["city_candidate"] = raw["city_candidate"]
            if candidate:
                saved["location_text"] = "в " + candidate[3]
        if raw.get("date_error"):
            values.pop("checkin", None)
            values.pop("checkout", None)
            saved["invalid_text"] = text
        elif "checkin" in incoming and "checkout" in incoming:
            saved["invalid_text"] = None

        app.store.db.execute(
            "INSERT INTO local_draft_inputs VALUES(?,?) "
            "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
            (draft_id, json.dumps(saved, ensure_ascii=False)),
        )

    if not row and raw["intent"] == "search" and not saved.get("child_age_pending"):
        return text
    if (
        not raw["values"]
        and adult is None
        and room_count is None
        and child is None
        and not raw.get("date_error")
        and not hotel_preferences
        and not scopes
    ):
        return text

    # Produce a deterministic local-only request from explicitly known slots.
    parts = ["Подбери отель"]
    if values.get("city"):
        parts.append("в " + values["city"])
    if values.get("country"):
        parts.append(values["country"])
    if values.get("checkin") and values.get("checkout"):
        parts.append("с " + values["checkin"] + " по " + values["checkout"])
    adults = fragments.get("adults")
    count = fragments.get("room_count")
    if adults is not None:
        parts.append(str(adults) + " взрослых")
    if count is not None:
        parts.append(str(count) + " номер")
    if saved.get("child_age_pending"):
        parts.append(str(saved.get("child_count", 1)) + " детей")
    else:
        ages = fragments.get("children")
        if ages:
            if len(ages) == 1:
                parts.append("ребёнок " + str(ages[0]) + " лет")
            else:
                parts.append("дети " + " и ".join(map(str, ages)) + " лет")
        # No child evidence is not an explicit request for no children.
    if values.get("budget"):
        parts.append("бюджет на отель до " + str(values["budget"]))
    if values.get("currency"):
        parts.append(values["currency"])
    if values.get("breakfast_wish"):
        parts.append("Желательно с завтраком")
    elif values.get("breakfast") is True:
        parts.append("нужен завтрак")
    elif values.get("breakfast") is False:
        parts.append("без завтрака")
    # Retain the exact user clauses as evidence; never replace them with model facts.
    parts.extend(saved.get("hotel_preferences", [])[-8:])
    if saved.get("invalid_text"):
        parts.append(saved["invalid_text"])
    if saved.get("location_text"):
        parts.append(saved["location_text"])
    parts.extend(unresolved.values())
    merged = ", ".join(parts)
    return merged if len(merged) <= 1500 else text


NEED_LABELS = {
    "city": "город",
    "country": "страна",
    "checkin": "дата заезда",
    "checkout": "дата выезда",
    "rooms": "число взрослых и распределение по номерам",
    "budget": "бюджет на отель",
    "currency": "валюта бюджета",
    "child_age": "возраст каждого ребёнка",
    "room_allocation": "распределение гостей по номерам",
    "budget_conflict": "однозначный бюджет",
    "guests_conflict": "однозначный состав гостей",
    "invalid_dates": "корректные даты",
    "request": "условия поездки",
}


def clarification_message(app, draft_id, intent, text):
    from natural_language import detect_date_error

    saved = {}
    if draft_id:
        with app.lock:
            table = app.store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_draft_inputs'"
            ).fetchone()
            row = (
                app.store.db.execute(
                    "SELECT body FROM local_draft_inputs WHERE id=?", (draft_id,)
                ).fetchone()
                if table
                else None
            )
            if row:
                saved = json.loads(row[0])
    values = saved.get("values", {})
    known = []
    if values.get("city"):
        display = {
            "Istanbul": "Стамбул", "Dubai": "Дубай", "Paris": "Париж",
            "New York": "Нью-Йорк", "Frankfurt am Main": "Франкфурт-на-Майне",
            "Moscow": "Москва", "Kazan": "Казань", "Rome": "Рим",
            "Berlin": "Берлин", "Amsterdam": "Амстердам",
            "Bern": "Берн", "Salzburg": "Зальцбург", "Turin": "Турин",
            "Saint Petersburg": "Санкт-Петербург",
        }
        known.append(display.get(values["city"], values["city"]))
    if values.get("checkin") and values.get("checkout"):
        known.append(values["checkin"] + " — " + values["checkout"])
    party = saved.get("guest_parts", {})
    if party.get("adults") is not None:
        known.append(str(party["adults"]) + (" взрослый" if party["adults"] == 1 else " взрослых"))
    if party.get("room_count") is not None:
        known.append(str(party["room_count"]) + " номер(а)")
    if party.get("children"):
        known.append(
            str(len(party["children"])) + " детей; возраст детей: "
            + ", ".join(map(str, party["children"]))
        )
    if values.get("budget"):
        known.append("бюджет " + str(values["budget"]) + " " + str(values.get("currency", "")))
    if saved.get("child_age_pending") and saved.get("child_count"):
        known.append(
            str(saved["child_count"])
            + " детей; возрастов указано: "
            + str(len(saved.get("partial_child_ages", [])))
        )
    diagnostic = detect_date_error(text)
    candidate = intent.get("city_candidate")
    needs = list(intent.get("needs") or [])
    # A partly collected guest allocation is a single missing slot, not a
    # reason to ask again for adults and room count already in the draft.
    if party.get("adults") is not None and party.get("room_count") == 1 and "child_age" in needs:
        needs = [n for n in needs if n != "rooms"]

    def need_label(name):
        if name == "rooms":
            if party.get("adults") is not None and party.get("room_count") is None:
                return "число номеров"
            if party.get("adults") is None and party.get("room_count") is not None:
                return "число взрослых"
            if (party.get("room_count") or 0) > 1:
                return "распределение гостей по номерам"
        return NEED_LABELS.get(name, name.replace("_", " "))

    labels = list(dict.fromkeys(need_label(n) for n in needs))
    return (
        ("Сохранено: " + "; ".join(known) + ". " if known else "")
        + ("Ошибка в датах: " + diagnostic + ". " if diagnostic else "")
        + ("Возможно, вы имели в виду " + candidate + "? " if candidate else "")
        + "Уточните: "
        + ", ".join(labels)
        + "."
    )


def preference_fragments(text):
    # Draft conditions are synthesized from explicit slots. Do not reinsert a
    # whole earlier search (including dates later corrected) as a hotel wish.
    from hotel_requirements import extract
    from pet_preferences import parse as pet_parse

    phrases = {
        "pool": "Обязательно нужен бассейн",
        "parking": "Обязательно нужна парковка",
        "air_conditioning": "Обязательно нужен кондиционер",
        "quiet": "Обязательно нужны тихие номера",
    }
    out = []
    if pet_parse(text) in ("required", "absent", "ambiguous"):
        from pet_preferences import MENTION

        # Keep verbatim pet clauses without replaying stale dates/budgets from
        # a previous full search into a later draft correction.
        out.extend(p.strip() for p in re.split(r"[,.;!\n]", text) if MENTION.search(p))
    for key, value in extract(text).items():
        if key == "min_stars":
            out.append("Не ниже " + str(value) + " звёзд")
        elif key == "max_distance_km":
            out.append("Не дальше " + str(value) + " км")
        elif key in phrases:
            out.append(phrases[key])
    for pattern in (
        r"желательно с завтраком",
        r"желательно[^,.;\n]*(?:бассейн|парковк[а-я]*|кондиционер)",
        r"вид на море",
        r"у моря",
        r"в центре",
        r"рядом с морем",
    ):
        for match in re.finditer(pattern, text, re.I):
            phrase = match.group(0)
            if not re.search(r"\d|подбери|найди|бюджет", phrase, re.I):
                out.append(phrase)
    return list(dict.fromkeys(out))
