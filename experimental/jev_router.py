"""Jev proposals are checked in memory. This module cannot persist or apply a session."""

from pathlib import Path
import sys, re, json
from dataclasses import asdict, replace

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "prototype/base"),
    str(Path(__file__).resolve().parents[1] / "prototype"),
]
from travel_core import Request, Guests, InputError
from natural_language import amount_in_text, explicit_dates

ACTIONS = {
    "PATCH_BUDGET_PREVIEW",
    "CLARIFY_DATES",
    "SAVE_PREFERENCE_PREVIEW",
    "GLM_FOR_COMPLEX_REQUEST",
    "INSPECT_EXISTING",
}


def snapshot(request):
    return json.loads(json.dumps(asdict(request), default=str))


def preview(text, action, current):
    if not isinstance(text, str) or not 1 <= len(text) <= 1500 or action not in ACTIONS:
        raise InputError("JEV_PROPOSAL_INVALID")
    values = {
        **current,
        "rooms": tuple(Guests(r["adults"], tuple(r["children"])) for r in current["rooms"]),
        "allowed_countries": tuple(current.get("allowed_countries", [])),
    }
    request = Request(**values)
    request.validate()
    result = {
        "model_proposed_action": action,
        "validated_action": action,
        "user_state_changed": False,
        "applied": False,
        "hotel_claims": [],
        "request_before": snapshot(request),
    }
    budget = amount_in_text(text)
    ambiguous = (
        bool(re.search(r"следующ\w*\s+пятниц|следующ\w*\s+недел|на выходн|числа\b", text, re.I))
        and explicit_dates(text) is None
    )
    preference = bool(re.search(r"тих\w*.*реб[её]н|реб[её]н\w*.*тих", text, re.I))
    if ambiguous:
        result.update(
            validated_action="CLARIFY_DATES",
            needs=["checkin", "checkout"],
            clarification="Назовите точную дату заезда и дату выезда с месяцем и годом. Даты не угаданы.",
        )
    elif preference:
        result.update(
            validated_action="SAVE_PREFERENCE_PREVIEW",
            preference_note={
                "text": text,
                "kind": "USER_WISH",
                "evidence_status": "UNVERIFIED",
                "applied": False,
            },
            needs=["child_age_if_guest_change", "quietness_evidence"],
            clarification="Пожелание зафиксировано отдельно. Тишина и пригодность гостиниц не подтверждены; возраст ребёнка и изменение гостей требуют уточнения.",
        )
    elif action == "PATCH_BUDGET_PREVIEW":
        if budget is None:
            raise InputError("JEV_BUDGET_NOT_EXPLICIT")
        proposed = replace(request, budget=budget)
        proposed.validate()
        result.update(
            proposed_patch={"budget": budget}, request_preview=snapshot(proposed), needs=[]
        )
    elif action in ("CLARIFY_DATES", "SAVE_PREFERENCE_PREVIEW"):
        raise InputError("JEV_ACTION_NOT_SUPPORTED_BY_TEXT")
    else:
        result.update(
            needs=["further_interpretation"] if action == "GLM_FOR_COMPLEX_REQUEST" else []
        )
    result["code_adjusted_model_action"] = result["validated_action"] != action
    return result


if __name__ == "__main__":
    data = json.loads(sys.stdin.read())
    print(
        json.dumps(
            preview(data["text"], data["action"], data["current"]), ensure_ascii=False, default=str
        )
    )
