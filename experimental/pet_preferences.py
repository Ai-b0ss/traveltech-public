"""Bounded pet travel constraint over confirmed preference text; no supplier claims.

The existing preference proposal owns confirmation/history. This module never
saves a session, guesses an animal's attributes, or verifies positive policy.
"""

import copy
import re

MENTION = re.compile(
    r"\b(?:собак\w*|п[её]с|пса|щен\w*|кошк\w*|кот(?:а|ом)?|питом\w*|животн\w*|pets?|dogs?|cats?)\b",
    re.I,
)
MESSAGE = (
    "Размещение с животным требует проверки: PET_POLICY_UNVERIFIED. "
    "Подходящее наличие и доплата за питомца UNKNOWN. "
    "Прежние цены и корзина STALE; нужен новый поиск и проверка правил конкретного отеля."
)
QUESTION = (
    "Уточните только условие о питомце: животное едет с вами и требуется его размещение в отеле?"
)


def parse(text):
    """None: unrelated; required/absent: explicit; ambiguous: ask, never guess.

    Negation and alternatives are deliberately narrow. In particular 'не без
    собаки' and 'не маленькая собака' cannot become a positive/negative guess.
    """
    if not isinstance(text, str) or not MENTION.search(text):
        return None
    if re.search(r"\b(?:можно|разрешен\w*|разреш[а-я]*|правила)\b|отел\w*\s+без\b", text, re.I):
        return "ambiguous"
    if re.search(
        r"\b(?:или|либо)\b|\bне\s+без\b|\bне\s+(?:маленьк|больш|крупн|нуж|треб|хот|бер|возьм|ед|добав)",
        text,
        re.I,
    ):
        return "ambiguous"
    negative = bool(
        re.search(
            r"\bбез\s+(?:собак\w*|кош\w*|кот\w*|питом\w*|животн\w*)|"
            r"\b(?:собак\w*|кош\w*|кот\w*|питом\w*|животн\w*)\s+не\s+(?:едет|бер[её]м|будет)|"
            r"\b(?:убери|исключи)\s+(?:собак\w*|кош\w*|кот\w*|питом\w*|животн\w*)|\bno\s+pets\b",
            text,
            re.I,
        )
    )
    # A positive and a negative in the same turn require clarification.
    positive = bool(
        re.search(
            r"\b(?:с|со)\s+(?:собак\w*|псом|кош\w*|кот\w*|питом\w*|животн\w*)|"
            r"\bдобав\w*\b[^.;!?]{0,80}\b(?:собак\w*|кош\w*|кот\w*|питом\w*|животн\w*)|"
            r"\b(?:размещени\w*|проживани\w*)\s+животн\w*|\bpet[- ]friendly\b",
            text,
            re.I,
        )
    )
    if negative and positive:
        return "ambiguous"
    if negative:
        return "absent"
    if positive:
        return "required"
    return "ambiguous"


def preference(text):
    return {"text": text, "evidence_status": "UNVERIFIED"}


def guest_text(text):
    """Exclude standalone pet clauses from human age/negation validation.

    Mixed clauses containing people stay intact and fail closed if ambiguous.
    Source text retained in preferences/drafts is never rewritten by this view.
    """
    if not MENTION.search(text):
        return text
    clauses = re.split(r"[,.;!?\n]", text)
    return ", ".join(
        clause
        for clause in clauses
        if not MENTION.search(clause)
        or re.search(r"взросл|реб[её]н|\bдет(?:ей|и)\b|номер", clause, re.I)
    )


def active(preferences):
    latest = None
    for item in preferences or []:
        text = item.get("text", "") if isinstance(item, dict) else item
        state = parse(text)
        if state in ("required", "absent"):
            latest = {"travelling_with_pet": state == "required", "text": text}
    return latest


def archived_policy(provider, identity):
    """Only a hotel-ID-bound captured policy can supply negative evidence."""
    adapter = getattr(provider, "adapter", None)
    detail = getattr(adapter, "detail", {})
    hotel = (detail.get("response") or {}).get("data", {}).get("hotel", {})
    if str(hotel.get("hotel_id")) != str(identity[1]):
        return {}
    policy = (hotel.get("policies") or {}).get("know_before_you_go", "")
    if isinstance(policy, str) and re.search(
        r"\bno pets\b|\bpets (?:are )?not allowed\b", policy, re.I
    ):
        return {
            "pet_policy_evidence": {
                "value": "NO_PETS",
                "quote": policy,
                "field": "policies.know_before_you_go",
                "hotel_id": str(identity[1]),
                "source": str(provider.details.relative_to(provider.rates.parents[2])).replace(
                    "\\", "/"
                ),
                "evidence_status": "ARCHIVED_RESPONSE",
            }
        }
    return {}


def gate_card(card):
    out = copy.deepcopy(card)
    evidence = out.get("pet_policy_evidence") or {}
    identity = out.get("identity") or []
    forbidden = (
        evidence.get("value") == "NO_PETS"
        and len(identity) > 1
        and evidence.get("hotel_id") == str(identity[1])
        and evidence.get("source")
        and evidence.get("field")
    )
    reason = "PET_POLICY_INCOMPATIBLE" if forbidden else "PET_POLICY_UNVERIFIED"
    out.update(
        status="REJECT" if forbidden or out.get("status") == "REJECT" else "REVIEW",
        recommendable=False,
        bookable=False,
        hard_requirements_satisfied=False,
        availability_verified=False,
        payable_price_confirmed=False,
        pet_policy_status="INCOMPATIBLE" if forbidden else "PET_POLICY_UNVERIFIED",
        pet_inventory="UNKNOWN",
        pet_fees="UNKNOWN",
        price_status="STALE",
    )
    out["reasons"] = list(dict.fromkeys([*out.get("reasons", []), reason, "PET_FEES_UNKNOWN"]))
    out.setdefault("hard_requirement_checks", {})["pet_required"] = {
        "required": True,
        "observed": "NO_PETS" if forbidden else None,
        "satisfied": False if forbidden else None,
        "label": "размещение животного",
        "evidence": evidence if forbidden else None,
    }
    if isinstance(out.get("availability_check"), dict):
        out["availability_check"].update(
            availability_verified=False, can_accept_changed_quote=False
        )
    out["reason_texts"] = [
        *out.get("reason_texts", []),
        "В архиве явно запрещены животные" if forbidden else "Размещение животного не подтверждено",
        "Доплата за питомца неизвестна; прежняя цена требует проверки",
    ]
    if "_reasons" in out:
        out["_reasons"] = out["reasons"]
    return out


def project(output, preferences, *, pending=False):
    """Project confirmed (or explicitly labelled pending) state without changing source."""
    state = active(preferences)
    if state is None:
        return output
    output["pet_constraint"] = {
        **state,
        "confirmation": "PENDING" if pending else "CONFIRMED",
        "pet_policy_status": "PET_POLICY_UNVERIFIED",
        "inventory": "UNKNOWN",
        "fees": "UNKNOWN",
    }
    if state["travelling_with_pet"]:
        for field in ("cards", "prospective_cards"):
            if field in output:
                output[field] = [gate_card(c) for c in output[field]]
        output.update(
            price_status="STALE", cart_status="STALE", pet_policy_status="PET_POLICY_UNVERIFIED"
        )
        if "selected_offer" in output:
            output["selected_offer"] = None
        output["summary"] = MESSAGE
        if "status_impact" in output:
            output["status_impact"] = MESSAGE
    return output


def annotate_turn(output, text, intent):
    state = parse(text)
    if state is None:
        return output
    if intent["intent"] == "clarify":
        output["pending_pet_constraint"] = {
            "text": text,
            "state": state,
            "confirmation": "UNCONFIRMED",
        }
        if "pet_policy" in intent["needs"]:
            output["language_message"] = QUESTION + " Текущие условия сохранены."
        else:
            output["language_message"] = (
                output.get("language_message", "")
                + " Условие о питомце ещё не подтверждено: "
                + text
            )
    elif output.get("has_preview"):
        output["language_message"] = (
            output.get("language_message", "")
            + " Условие о питомце применится только после подтверждения Preview. "
            + MESSAGE
        )
    return output
