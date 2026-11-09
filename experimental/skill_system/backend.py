"""Domain operations delegate to the existing request, pricing and proposal engines."""

import request_deadline as deadline
import hashlib, re, math
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from app import scenario, jsonable, REASONS
from natural_language import validate_intent, amount_in_text, REQUIRED, local_rules
from budget_math import resolve_budget
from refinement import normalize
from travel_core import Engine, InputError
from city_names import canonical_city, COUNTRIES
from provider_transport import Failure, canonical


def validate_proposal(proposal, text, current):
    evidence = proposal["evidence"]
    values = proposal["values"]
    if set(evidence) - set(values):
        raise Failure("EVIDENCE_UNKNOWN_FIELD")
    for field in values:
        quote = evidence.get(field)
        if not isinstance(quote, str) or not quote.strip() or quote not in text:
            raise Failure("SOURCE_SPAN_REQUIRED:" + field)
    raw = {
        "intent": proposal["intent"],
        "values": dict(values),
        "needs": proposal["needs"],
        **{k: proposal[k] for k in ("date_error", "city_candidate") if k in proposal},
    }
    adjusted = False
    kind = raw["intent"]
    if kind == "clarify" and current is None and local_rules(text, None).get("intent") == "search":
        raise Failure("CLARIFICATION_OF_ALREADY_COMPLETE_SEARCH")
    # A model cannot ask for a budget already unambiguously stated in this
    # follow-up. Reject its proposal so the bounded repair path can respond.
    if kind == "clarify" and current is not None and "budget" in raw["needs"]:
        observed = local_rules(text, current)
        if observed.get("intent") == "budget" and observed.get("needs") == []:
            raise Failure("CLARIFICATION_OF_EXPLICIT_BUDGET")
    # Models identify the requested operation; Decimal arithmetic belongs to backend.
    # Recompute only when a budget field is actually proposed and independently present in user text.
    if "budget" in raw["values"]:
        target = (
            amount_in_text(text)
            if kind == "search"
            else resolve_budget(text, current.budget, currency=current.currency)
            if current is not None and kind in ("budget", "refine")
            else None
        )
        if (
            target is not None
            and target not in ("CONFLICT", "INVALID")
            and raw["values"]["budget"] != target
        ):
            raw["values"]["budget"] = target
            adjusted = True
    if kind == "search":
        original_needs = raw["needs"]
        ignore = set(REQUIRED) & set(raw["values"])
        if re.search(r"бассейн", text, re.I):
            ignore.update(("pool_availability", "pool_preference"))
        if re.search(r"недалеко\s+от\s+моря|рядом\s+с\s+морем|у\s+моря", text, re.I):
            ignore.update(("sea_distance", "sea_distance_preference"))
        if re.search(r"тих", text, re.I):
            ignore.add("quietness_evidence")
        raw["needs"] = [x for x in original_needs if x not in ignore]
        adjusted = adjusted or raw["needs"] != original_needs
    # An unchanged currency redundantly copied from context is not a requested mutation.
    # Cross-currency guesses remain forbidden by the domain validator.
    if (
        current is not None
        and kind in ("budget", "refine")
        and "currency" in raw["values"]
        and raw["values"]["currency"] == current.currency
    ):
        raw["values"].pop("currency")
        adjusted = True
    try:
        intent, request, name = validate_intent(raw, text, current)
    except InputError as exc:
        raise Failure(str(exc)) from exc
    if intent["intent"] == "clarify" and not intent["needs"]:
        raise Failure("CLARIFY_NEEDS_REQUIRED")
    if intent["intent"] == "preference" and not current:
        request = None
    if intent["intent"] in ("budget", "constraints", "refine"):
        request = normalize(current, intent["values"])
    if request is not None:
        try:
            request.validate()
        except InputError as exc:
            raise Failure(str(exc)) from exc
    from .answer_grounding import validate_spans

    validate_spans(intent, evidence, text, current)
    return intent, request, name, adjusted or raw != intent


def search_preferences(text):
    """Keep explicitly stated soft wishes as UNVERIFIED, independently of model output."""
    pattern = r"желательн|предпочтительн|недалеко\s+от|поближ[е]|рядом\s+с|у\s+моря|в\s+центр[еа]|бассейн|парковк|кондиционер|тих|тишин|спокойн|вид\s+на|пешком|зв[её]зд|не\s+кур"
    from pet_preferences import parse

    return [text] if re.search(pattern, text, re.I) or parse(text) in ("required", "absent") else []


def invalidations(before, after):
    if before is None:
        return ["hotel_candidates", "room_rates", "availability", "comparison", "weather"]
    changes = {k for k in after if before.get(k) != after[k]}
    out = set()
    if changes & {"city", "country"}:
        out.update(["hotel_candidates", "room_rates", "availability", "comparison", "weather"])
    if changes & {"checkin", "checkout", "rooms"}:
        out.update(["room_rates", "availability", "comparison"])
    if changes & {"checkin", "checkout"}:
        out.add("weather")
    if changes & {"budget", "currency", "breakfast", "free_cancel_until"}:
        out.add("comparison")
    return sorted(out)


def resolve_live_location(request, resolution):
    aliases = [request.city.casefold()]
    from city_names import ALIASES, RUSSIAN_ALIASES

    for label, entry in ALIASES.items():
        if entry[0].casefold() == request.city.casefold() and entry[1] == request.country:
            aliases.append(label)
    for label, entry in RUSSIAN_ALIASES.items():
        if entry[0].casefold() == request.city.casefold() and entry[1] == request.country:
            aliases.append(label)
    if request.city.casefold() in ("сочи", "sochi"):
        aliases += ["索契"]
    if request.city.casefold() == "dubai":
        aliases += ["迪拜", "杜拜", "دبي"]
    if request.city.casefold() == "istanbul":
        aliases += ["伊斯坦布尔"]
    countries = {
        "RU": ["russia", "russian federation", "россия", "俄罗斯"],
        "AE": ["united arab emirates", "оаэ", "阿拉伯联合酋长国"],
        "TR": ["turkey", "türkiye", "турция", "土耳其"],
    }.get(request.country, [COUNTRIES[request.country].casefold()])
    matches = []
    for region in resolution.get("regions") or []:
        label = (region.get("name") or "").casefold().strip()
        nation = (region.get("country") or "").casefold()
        region_type = (region.get("region_type") or "").casefold()
        if (
            region.get("region_id") is not None
            and region_type == "city"
            and label in aliases
            and any(c in nation for c in countries)
        ):
            matches.append(region)
    matches = {str(x["region_id"]): x for x in matches}
    if len(matches) > 1:
        raise Failure("LOCATION_NEEDS_EXPLICIT_SELECTION")
    if len(matches) == 1:
        region = next(iter(matches.values()))
        return {
            "mode": "region",
            "region_id": str(region["region_id"]),
            "location_name": region.get("full_name") or region["name"],
        }, "COMPLETE_REGION"
    place = resolution.get("place") or {}
    name = (place.get("name") or "").casefold().strip()
    address = (place.get("formatted_address") or "").casefold()
    lat, lon = place.get("latitude"), place.get("longitude")
    if (
        name not in aliases
        or not any(c in address for c in countries)
        or type(lat) not in (float, int)
        or type(lon) not in (float, int)
        or not math.isfinite(lat)
        or not math.isfinite(lon)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        raise Failure("LOCATION_NEEDS_EXPLICIT_SELECTION")
    radius = place.get("recommended_radius_km", 3)
    if type(radius) not in (float, int) or not math.isfinite(radius) or not 0 < radius <= 20:
        raise Failure("LOCATION_RADIUS_INVALID")
    return {
        "mode": "nearby",
        "latitude": lat,
        "longitude": lon,
        "radius_km": radius,
        "location_name": place["name"],
    }, "PARTIAL_CITY_CENTER_NEARBY"


class Backend:
    def __init__(self, app):
        self.app = app

    def snapshot(self, sid, revision):
        with self.app.lock:
            deadline.check()
            if not sid:
                return None, None, []
            name, s = self.app._load(sid)
            if type(revision) is not int or revision != s.state.revision:
                raise Failure("STALE_REVISION")
            return name, s.state.request, self.app.store.get_session(sid).get("preferences", [])

    def recheck_revision(self, sid, revision):
        self.snapshot(sid, revision)

    def search(self, request, name, mode, requirements=None):
        data = jsonable(request)
        if mode == "live":
            from supplier_router import active_supplier

            routed = active_supplier() != "TOURMIND" and getattr(
                self.app.public, "supports_supplier_router", False
            )
            if routed:
                location = None
                scope = "PARTIAL_SUPPLIER_CATALOG"
                result = self.app.public.search(data, location)
            else:
                resolution = self.app.public.location(request.city)
                location, scope = resolve_live_location(request, resolution)
                result = self.app.public.search(data, location)
            result["hotel_requirements"] = requirements or {}
            enrich = getattr(self.app.public, "enrich_details", None)
            if callable(enrich):
                result = enrich(result, requirements)
            from offer_state import project

            from .source_compaction import compact_live_source
            result = compact_live_source(result, request)
            common, checked = project(data, result)
            by_identity = {tuple(c["identity"][1:4]): c for c in common}
            for offer in result.get("offers", []):
                c = by_identity[tuple(offer[k] for k in ("hotel_id", "room_id", "rate_code"))]
                offer.update(
                    offer_id=c["offer_id"],
                    status=c["status"],
                    reasons=c["reasons"],
                    recommendable=c["recommendable"],
                    hard_requirement_checks=c["hard_requirement_checks"],
                )
            result["location_coverage"] = (
                ("PARTIAL_WITHIN_" + str(location["radius_km"]) + "KM_CITY_CENTER")
                if scope == "PARTIAL_CITY_CENTER_NEARBY"
                else "PARTIAL_SUPPLIER_CATALOG"
                if routed
                else "MATCHED_REGION"
            )
            offers = [
                {
                    **o,
                    "_key": str(i),
                    "_name": o.get("hotel_name", "Отель"),
                    "_price": o.get("total"),
                    "_currency": o.get("currency"),
                    "_reasons": o.get("reasons", []),
                }
                for i, o in enumerate(result.get("offers", []))
            ]
            return offers, {
                "mode": result.get("source") if routed else "TOURMIND_LIVE_RATE",
                "status": result["status"],
                "observed_at": result.get("observed_at"),
                "availability_verified": False,
                "location_coverage": result["location_coverage"],
                "coverage": result.get("coverage"),
                "result": result,
            }
        if not name:
            name = canonical_city(request.city, request.country)[2]
        if name == "live_dynamic":
            return [], {
                "mode": "archive", "status": "SOURCE_NOT_REQUESTED",
                "availability_verified": False,
                "reason": "OFFLINE_ARCHIVE_UNAVAILABLE_FOR_DYNAMIC_CITY",
            }
        _, supplier = scenario(name, datetime.now(timezone.utc))
        result = jsonable(
            Engine(supplier, max_age=timedelta(hours=24)).search(
                request, datetime.now(timezone.utc)
            )
        )
        if result["status"] == "SOURCE_ERROR":
            raise Failure("SOURCE_ERROR_NO_SYNTHETIC_FALLBACK")
        offers = [
            {
                **o,
                "_key": str(i),
                "_name": "Тариф " + str(o["identity"][1]),
                "_price": o.get("base_total"),
                "_currency": o.get("currency"),
                "_reasons": o.get("reasons", []),
            }
            for i, o in enumerate(result.get("decisions", []))
        ]
        from pet_preferences import archived_policy

        for offer in offers:
            offer.update(archived_policy(supplier, offer["identity"]))
        if requirements:
            from hotel_requirements import check

            offers = [check(o, requirements) for o in offers]
            for o in offers:
                o["_reasons"] = o["reasons"]
        return offers, {
            "mode": "SYNTHETIC" if name in ("synthetic", "empty", "error") else "ARCHIVED_RESPONSE",
            "status": result["status"],
            "availability_verified": False,
        }

    def evidence(self, offers, source, request, run_id, text=""):
        from .offer_shortlist import shortlist

        limit = 24 if source.get("result") is not None else max(1, len(offers))
        ranked = shortlist(offers, request, max_count=limit, preferences_text=text)
        if source.get("mode") == "TOURMIND_LIVE_RATE":
            source["shortlist_policy"] = (
                "Evidence-only: verified conditions, comparable currency and one best rate per hotel before additional rates; unverified wishes never scored."
            )
        facts = {}
        cards = {}
        scope = hashlib.sha256(
            canonical({"run": run_id, "request": jsonable(request)}).encode()
        ).hexdigest()[:12]
        for i, o in enumerate(ranked):
            oid = o.get("offer_id") or scope + ":O" + str(i + 1)
            cards[oid] = o
            facts[oid + ":name"] = {
                "offer_id": oid,
                "field": "hotel_name",
                "text": str(o["_name"]),
                "source": source["mode"],
            }
            fid = oid + ":status"
            facts[fid] = {
                "offer_id": oid,
                "field": "status",
                "text": o["status"],
                "source": source["mode"],
            }
            if o.get("_price") is not None:
                facts[oid + ":price"] = {
                    "offer_id": oid,
                    "field": "price",
                    "text": str(o["_price"]) + " " + str(o["_currency"]) + " за проживание",
                    "source": source["mode"],
                }
            for field, entry in (o.get("supplier_facts") or {}).items():
                if (
                    field not in ("star_rating", "distance_to_search_anchor_km", "meal_count")
                    or type(entry) is not dict
                ):
                    continue
                facts[oid + ":supplier:" + field] = {
                    "offer_id": oid,
                    "field": field,
                    "text": str(entry.get("value"))
                    + " (поле "
                    + str(entry.get("field"))
                    + "; без вывода неподтверждённых удобств)",
                    "source": entry.get("source", "TOURMIND_RATE"),
                }
            for j, reason in enumerate(o["_reasons"]):
                facts[oid + ":R" + str(j + 1)] = {
                    "offer_id": oid,
                    "field": "reason",
                    "text": reason,
                    "source": source["mode"],
                }
        return cards, facts

    def select_grounded(self, cards, facts, limit=3):
        # Hotel order is already determined by offer_shortlist from verified
        # source conditions. Picking up to three diverse hotels is arithmetic,
        # not an LLM decision; all descriptions still come from source facts.
        if type(limit) is not int or not 1 <= limit <= 10:
            raise Failure("EVIDENCE_SELECTION_LIMIT")
        eligible = [
            (key, offer)
            for key, offer in cards.items()
            if offer["status"] != "REJECT" and offer.get("recommendable") is not False
        ]
        chosen = []
        seen = set()
        for key, offer in eligible:
            hotel = (offer.get("_name") or key).casefold()
            if hotel in seen:
                continue
            chosen.append(key)
            seen.add(hotel)
            if len(chosen) >= limit:
                break
        for key, _ in eligible:
            if len(chosen) >= min(limit, len(eligible)):
                break
            if key not in chosen:
                chosen.append(key)
        reasons = [
            fid
            for key in chosen
            for fid, fact in facts.items()
            if fact.get("offer_id") == key and fact.get("field") == "reason"
        ][:12]
        selection = {"offer_ids": chosen, "reason_ids": reasons}
        self.present(selection, cards, facts, {"mode": "GROUNDED_SOURCE", "status": "OK"})
        return selection

    def explain(self, selection, cards, facts, source):
        selected = [cards[k] for k in selection["offer_ids"]]
        if any(o["status"] == "REJECT" or o.get("recommendable") is False for o in selected):
            raise Failure("REJECTED_OFFER_CANNOT_BE_RECOMMENDED")
        if any(facts[k]["offer_id"] not in selection["offer_ids"] for k in selection["reason_ids"]):
            raise Failure("EVIDENCE_OFFER_MISMATCH")
        eligible = [
            o
            for o in cards.values()
            if o["status"] != "REJECT" and o.get("recommendable") is not False
        ]
        if eligible and not selected:
            raise Failure("EMPTY_SELECTION_WITH_ELIGIBLE_OFFERS")
        count_total = (
            len((source.get("result") or {}).get("offers") or [])
            if source.get("mode") == "TOURMIND_LIVE_RATE"
            else len(cards)
        )
        source_label = {
            "archive": "офлайн-режим без доступного архива для этого города",
            "TOURMIND_LIVE_RATE": "TourMind · ответ поставщика",
            "LITEAPI_SANDBOX_RATE": "LiteAPI · тестовая среда",
            "LITEAPI_RATE_OBSERVATION": "LiteAPI · наблюдение тарифа",
            "TREG_DISCOVERY_UNVERIFIED": "разведка без подтверждённых тарифов",
            "ARCHIVED_RESPONSE": "сохранённый архив",
            "SYNTHETIC": "тестовые примеры",
        }.get(source["mode"], source["mode"])
        lines = (
            [
                "Поставщик вернул "
                + str(count_total)
                + " тарифов; для сравнения отобрано "
                + str(len(cards))
                + " предложений с охватом разных гостиниц. Полный результат сохранён отдельно."
            ]
            if count_total > len(cards)
            else ["Сопоставлены " + str(len(cards)) + " тарифов. Источник: " + source_label + "."]
        )
        statements = [
            {
                "text": lines[0],
                "kind": "SOURCE_SUMMARY",
                "fact_ids": [],
                "basis": {
                    "source_mode": source["mode"],
                    "compared_offers": len(cards),
                    "supplier_rates": count_total,
                },
            }
        ]
        if str(source.get("location_coverage", "")).startswith("PARTIAL_WITHIN_"):
            lines.append(
                "Поиск ограничен радиусом "
                + str(
                    (source.get("result") or {}).get("location", {}).get("radius_km", "неизвестно")
                )
                + " км от центра подтверждённого города, это не полная выдача по всему городу."
            )
        if len(lines) > 1:
            statements.append({"text": lines[-1], "kind": "SOURCE_SCOPE", "fact_ids": []})
        for oid in selection["offer_ids"]:
            o = cards[oid]
            from hotel_requirements import reason_text

            labels = {
                **REASONS,
                "AVAILABILITY_NOT_CHECKED": "наличие не проверено",
                "FINAL_PAYABLE_PRICE_UNVERIFIED": "окончательный счёт не подтверждён",
                "SANDBOX_NOT_LIVE_INVENTORY": "тестовая среда, реальные цена и наличие не подтверждены",
                "SUPPLIER_OCCUPANCY_UNVERIFIED": "состав гостей отдельно не подтверждён",
                "SUPPLIER_DATES_REQUEST_BOUND": "даты привязаны к запросу, отдельное подтверждение поставщика отсутствует",
            }
            price = (
                o["_name"]
                + ": "
                + str(o["_price"] if o.get("_price") is not None else "стоимость неизвестна")
                + " "
                + str(o.get("_currency") or "")
                + "."
            )
            caution = (
                "Требует проверки: "
                + ", ".join(labels.get(r, reason_text(r)) for r in o["_reasons"])
                + "."
                if o["_reasons"]
                else "Условия проверены доступными данными."
            )
            price_ids = [
                k
                for k, v in facts.items()
                if v["offer_id"] == oid and v.get("field") in ("hotel_name", "price")
            ]
            reason_ids = [
                k for k, v in facts.items() if v["offer_id"] == oid and v.get("field") == "reason"
            ]
            status_ids = [
                k for k, v in facts.items() if v["offer_id"] == oid and v.get("field") == "status"
            ]
            statements.append(
                {"text": price, "kind": "OFFER_PRICE", "offer_id": oid, "fact_ids": price_ids}
            )
            statements.append(
                {
                    "text": caution,
                    "kind": "OFFER_CONDITIONS",
                    "offer_id": oid,
                    "fact_ids": reason_ids or status_ids,
                }
            )
            lines.extend([price, caution])
        supported_lines = len(lines)
        if source.get("status") == "SOURCE_NOT_REQUESTED":
            lines.append(
                "Архив для этого города недоступен; внешний поиск не выполнялся. "
                "Условия можно сохранить после подтверждения Preview. Цена и наличие неизвестны."
            )
        if source.get("status") == "NO_LIVE_OFFERS":
            lines.append(
                "Поставщик не нашёл доступных тарифов на эти даты в проверенной зоне. Это не доказательство отсутствия гостиниц во всём городе."
            )
        if source.get("status") == "DISCOVERY_ONLY":
            lines.append(
                "Разведка вернула кандидатов без подтверждённых гостиничных тарифов. Они не используются как доступные предложения."
            )
        if source.get("coverage") is not None:
            lines.append("Проверена ограниченная выборка гостиниц; это не полный поиск по городу.")
        elif not selected:
            lines.append("Подходящий вариант по доступным данным не установлен.")
        lines.append("Текущее наличие не подтверждено; бронирование не выполнено.")
        statements.extend(
            {"text": text, "kind": "POLICY", "fact_ids": []} for text in lines[supported_lines:]
        )
        return {
            "text": " ".join(lines),
            "statements": statements,
            "model_selected_fact_ids": list(selection["reason_ids"]),
        }

    def present(self, selection, cards, facts, source):
        return self.explain(selection, cards, facts, source)["text"]

    def prepare(self, intent, request, name, text, sid, revision, draft_id, source_result=None):
        with self.app.lock:
            deadline.check()
            self.recheck_revision(sid, revision)
            kind = intent["intent"]
            if kind == "search":
                p = self.app.preview_create(
                    name,
                    request,
                    sid,
                    revision,
                    search_preferences(text),
                    draft_id,
                    source_result=source_result,
                )
            elif kind in ("budget", "constraints", "refine"):
                p = self.app.preview_proposal(
                    sid,
                    patch=intent["values"],
                    revision=revision,
                    action=kind,
                    additional_preferences=search_preferences(text),
                    source_result=source_result,
                )
            elif kind == "preference" and sid:
                p = self.app.preview_preference(sid, intent["values"], revision)
            elif kind == "undo":
                return self.app.execute("undo", sid=sid, revision=revision)
            else:
                p = None
            out = (
                self.app.execute("read", sid=sid)
                if sid
                else {"session_id": None, "request": None, "revision": None, "cards": []}
            )
            out["has_preview"] = bool(p)
            if p:
                out.update(preview=p, preview_id=p["preview_id"])
            return out
