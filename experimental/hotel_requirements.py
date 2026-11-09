"""Explicit hard hotel requirements are gates, never merely ranking bonuses.

Unknown evidence remains unknown; hard unknowns are not recommendations.
Only typed, identity-bound supplier facts can satisfy these requirements.
"""

import re, copy, math

LABELS = {
    "min_stars": "звёздность",
    "pool": "бассейн",
    "parking": "парковка",
    "air_conditioning": "кондиционер",
    "quiet": "тишина",
    "pet_required": "размещение животного",
    "max_distance_km": "расстояние от точки поиска",
}


def extract(text):
    if not isinstance(text, str):
        return {}
    # Wishes do not become requirements. Evaluate individual clauses so a soft
    # view preference does not weaken an explicit mandatory pool in another.
    result = {}
    for clause in re.split(r"[.;!\n]|\bно\b", text, flags=re.I):
        soft = re.search(r"желательн|по возможности|не\s*обязательн|предпочтительн", clause, re.I)
        hard = re.search(
            r"обязател|нужен|нужна|нужно|только|не ниже|минимум|не дальше|не более", clause, re.I
        )
        star = re.search(
            r"(?:от|не ниже|минимум)\s*([1-5])\s*зв[её]зд|([1-5])\s*(?:зв[её]зд|\*)", clause, re.I
        )
        if star and not soft:
            result["min_stars"] = int(star.group(1) or star.group(2))
        if not hard or soft:
            continue
        for key, pattern in [
            ("pool", r"бассейн"),
            ("parking", r"парковк"),
            ("air_conditioning", r"кондиционер"),
            ("quiet", r"тих|тишин"),
        ]:
            if re.search(pattern, clause, re.I):
                result[key] = True
        distance = re.search(
            r"(?:не дальше|не более|максимум)\s*(\d+(?:[.,]\d+)?)\s*км", clause, re.I
        )
        if distance:
            result["max_distance_km"] = float(distance.group(1).replace(",", "."))
    return result


def combine(preferences, text):
    merged = {}
    for item in [*(preferences or []), text]:
        if isinstance(item, dict):
            item = item.get("text", "")
        for key, value in extract(item).items():
            if key in merged and merged[key] != value:
                # Two incompatible instructions must not silently weaken one.
                if key == "min_stars":
                    value = max(merged[key], value)
                elif key == "max_distance_km":
                    value = min(merged[key], value)
            merged[key] = value
    from pet_preferences import active

    pet = active([*(preferences or []), text])
    if pet and pet["travelling_with_pet"]:
        merged["pet_required"] = True
    return merged


def check(offer, requirements):
    out = copy.deepcopy(offer)
    facts = out.get("supplier_facts") or {}
    reasons = list(out.get("reasons") or [])
    checks = {}
    failed = False
    unknown = False
    for key, wanted in requirements.items():
        if key == "pet_required":
            # This bounded model cannot verify a positive pet policy or fee.
            # gate_card supplies the explicit NO_PETS/UNKNOWN check below.
            continue
        field = (
            "star_rating"
            if key == "min_stars"
            else "distance_to_search_anchor_km"
            if key == "max_distance_km"
            else key
        )
        entry = facts.get(field)
        value = (
            entry.get("value")
            if isinstance(entry, dict) and entry.get("source") and entry.get("field")
            else None
        )
        valid = (
            (type(value) in (int, float) and math.isfinite(value))
            if key in ("min_stars", "max_distance_km")
            else type(value) is bool
        )
        if key == "min_stars":
            valid = valid and 1 <= value <= 5 if valid else False
        if key == "max_distance_km":
            valid = valid and value >= 0 if valid else False
        met = (
            (
                value >= wanted
                if key == "min_stars"
                else value <= wanted
                if key == "max_distance_km"
                else value is wanted
            )
            if valid
            else None
        )
        checks[key] = {
            "required": wanted,
            "observed": value if valid else None,
            "satisfied": met,
            "label": LABELS.get(key, key),
            "evidence": entry if valid else None,
        }
        if met is False:
            failed = True
            reasons.append("HARD_REQUIREMENT_FAILED:" + key)
        elif met is None:
            unknown = True
            reasons.append("HARD_REQUIREMENT_UNKNOWN:" + key)
    out["hard_requirement_checks"] = checks
    out["hard_requirements_satisfied"] = not failed and not unknown
    out["recommendable"] = not failed and not unknown and out.get("status") != "REJECT"
    out["status"] = (
        "REJECT"
        if failed or out.get("status") == "REJECT"
        else "REVIEW"
        if unknown
        else out.get("status", "REVIEW")
    )
    out["reasons"] = list(dict.fromkeys(reasons))
    if requirements.get("pet_required"):
        from pet_preferences import gate_card

        out = gate_card(out)
    return out


def reason_text(reason):
    if reason.startswith("HARD_REQUIREMENT_FAILED:"):
        return "Обязательное условие не выполнено: " + LABELS.get(reason.split(":", 1)[1], reason)
    if reason.startswith("HARD_REQUIREMENT_UNKNOWN:"):
        return "Обязательное условие не подтверждено: " + LABELS.get(
            reason.split(":", 1)[1], reason
        )
    return reason
