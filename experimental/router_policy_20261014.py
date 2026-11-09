"""Conservative production-shape task router, deterministic guards before optional Jev.

Only OneAPI models enabled for LIVE. Reformboss is archived evidence ONLY.
Jev can advise (not decide), and is skipped on cases deterministic code can handle.
"""

import re
from dataclasses import dataclass
from natural_language import source_quote_inspection, conflict_needs
from refinement import propose as refined_intent


@dataclass(frozen=True)
class RouteDecision:
    route: str
    reason: str
    jev_used: bool = False
    suggested_jev_action: str | None = None
    ignored_jev: bool = False


PRIMARY = "OneAPI/glm-5.3-flash"
BACKUP = "OneAPI/mimo-v2.6-flash"


def classify(text, context=None, jev_suggestion=None):
    """No network; never trust Jev classification over local safety invariants."""
    if source_quote_inspection(text):
        return RouteDecision(
            "LOCAL_INSPECT",
            "UNTRUSTED_SOURCE_QUOTE_BLOCKS_MUTATION",
            False,
            jev_suggestion,
            bool(jev_suggestion),
        )
    if (
        conflict_needs(text)
        or re.search(r"\b(?:или|либо)\b", text, re.I)
        and re.search(r"(?:бюджет|тысяч|рубл)", text, re.I)
    ):
        return RouteDecision(
            "LOCAL_CLARIFY",
            "CONFLICTING_BUDGET_REQUIRES_CLARIFICATION",
            False,
            jev_suggestion,
            bool(jev_suggestion),
        )
    local = refined_intent(text, context)
    if local and local.get("intent") == "clarify":
        return RouteDecision(
            "LOCAL_CLARIFY",
            "LOCAL_CONFLICT_VALIDATION",
            False,
            jev_suggestion,
            bool(jev_suggestion),
        )
    if local and local.get("intent") == "inspect":
        return RouteDecision(
            "LOCAL_INSPECT",
            "EVIDENCE_QUESTION_NEVER_MODEL_ASSERTION",
            False,
            jev_suggestion,
            bool(jev_suggestion),
        )
    if local and local.get("intent") == "refine":
        # Even correct structured LLM output must pass the deterministic validator.
        return RouteDecision(
            PRIMARY,
            "MULTI_FIELD_REFINEMENT_NEEDS_STRUCTURED_VALIDATION",
            False,
            jev_suggestion,
            bool(jev_suggestion),
        )
    if (
        context
        and re.search(r"(?:снизь|снизи|увеличь|подними|уменьши|повысь)", text, re.I)
        and re.search(r"(?:бюджет|рубл|тыс|процент|%)", text, re.I)
        and not re.search(r"(?:дат|гост|номер|завтрак|город|заезд|выезд)", text, re.I)
    ):
        return RouteDecision(
            "LOCAL_BUDGET", "LOCAL_DECIMAL_ARITHMETIC", False, jev_suggestion, bool(jev_suggestion)
        )
    if re.search(r"(?:подбери|найди|сочи|дубай|стамбул)", text, re.I):
        return RouteDecision(
            PRIMARY,
            "GENERAL_SEARCH_USE_STRUCTURED_LLM",
            False,
            jev_suggestion,
            bool(jev_suggestion),
        )
    # Unknown and ambiguous: do not execute; optional bounded choice is advisory only.
    if jev_suggestion in ("INSPECT_EVIDENCE", "CLARIFY_CONFLICT"):
        return RouteDecision(
            "LOCAL_CLARIFY", "ADVISORY_JEV_NO_MUTATION", True, jev_suggestion, False
        )
    return RouteDecision(
        PRIMARY,
        "UNCERTAIN_GENERAL_PARSE_AND_VALIDATE",
        bool(jev_suggestion),
        jev_suggestion,
        bool(jev_suggestion),
    )


def live_routes():
    """Never point production requests to Reformboss until explicitly reenabled by owner."""
    return [("OneAPI", "glm-5.3-flash"), ("OneAPI", "mimo-v2.6-flash")]
