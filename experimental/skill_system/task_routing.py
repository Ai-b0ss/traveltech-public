"""Conservative routing by task TYPE, not speculative model intelligence.

Held-in-memory benchmark replay (same development set): all Qwen 15/16,
all IISHKO 13/16, Qwen for new search + IISHKO for follow-ups 15/16.
This is hypothesis-generating, not independently established generalization.
"""

import json, re
from .transport import gwarden_configuration_reason

PRIORITY = (
    "Gwarden_Gemini4Argon",
    "InkStone_AgentsA1",
)
LEGACY_PRIORITY = ("IISHKO", "InkStone_AgentsA1")



def context(messages):
    try:
        payload = json.loads(messages[-1]["content"])
        state = (payload.get("context") or {}).get("state") or {}
        return state if isinstance(state, dict) else {}
    except (ValueError, IndexError, KeyError, TypeError, AttributeError):
        return {}


def classify_request(messages):
    state = context(messages)
    text = state.get("request_text") or ""
    if not isinstance(text, str):
        return "FOLLOWUP"
    txt = text.casefold()
    if state.get("current_request") is None and re.search(
        r"подбери|найди|ищи|гостиниц|отел[ьяе]|размещени", txt
    ):
        return "NEW_SEARCH"
    return "FOLLOWUP"


def preferred_routes(messages):
    kind = classify_request(messages)
    # Preserve the known working model route until the owner has enabled
    # Gwarden with a valid local key and the limited-time free promotion.
    return (PRIORITY if gwarden_configuration_reason() is None else LEGACY_PRIORITY), kind
