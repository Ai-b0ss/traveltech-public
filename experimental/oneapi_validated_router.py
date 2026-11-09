"""OneAPI-only structured proposal router. Reformboss is NEVER called here.

A provider's answer is not a state mutation. Independently validate request
semantics before showing preview; only explicit app confirmation changes state.
No retries to the same model; fallback to a different, separately budgeted route.
"""

import json, time
from copy import deepcopy


def safe_semantic_normalization(proposed, accepted):
    """Only explicit city alias Sochi -> Сочи is a nontrivial safe transformation."""
    if proposed == accepted:
        return True
    if proposed.get("intent") != accepted.get("intent") or proposed.get("intent") != "search":
        return False
    alt = deepcopy(proposed)
    if (
        isinstance(alt.get("values"), dict)
        and alt["values"].get("city") == "Sochi"
        and alt["values"].get("country") == "RU"
    ):
        alt["values"]["city"] = "Сочи"
        return alt == accepted
    return False


def semantic_delta_paths(proposed, accepted):
    """Field paths, never model text or confidential values."""
    result = []
    for k in ("intent", "needs"):
        if proposed.get(k) != accepted.get(k):
            result.append(k)
    left = proposed.get("values", {})
    right = accepted.get("values", {})
    if isinstance(left, dict) and isinstance(right, dict):
        for k in sorted(left.keys() | right.keys()):
            if left.get(k) != right.get(k):
                result.append("values." + k)
    else:
        result.append("values")
    return result


from ai_router import ChatAdapter, AdapterError
from natural_language import validate_intent, local_rules
from app import request_from
from router_policy_20261014 import live_routes, classify


class OneAPIValidatedRouter:
    def __init__(self, transport, routes=None):
        self.transport = transport
        self.routes = routes or live_routes()
        if not self.routes or any(p != "OneAPI" for p, m in self.routes):
            raise ValueError("ONLY_ONEAPI_ROUTES_AUTHORIZED")
        self.history = []

    def infer(self, messages):
        try:
            data = json.loads(messages[-1]["content"])
            text = data["user_text"]
            context = request_from(data["current_request"]) if data.get("current_request") else None
        except (ValueError, TypeError, KeyError) as e:
            raise AdapterError("TRAVEL_CONTEXT_REQUIRED") from None
        policy = classify(text, context)
        trace = []
        before_calls = len(self.transport.attempts()) if hasattr(self.transport, "attempts") else 0
        for provider, model in self.routes:
            a = ChatAdapter(provider, model, self.transport)
            start = time.perf_counter()
            try:
                proposed = a.infer(messages)
                accepted, _, _ = validate_intent(proposed, text, context)
                # Safe semantic corrections from deterministic code are authoritative.
                if (
                    accepted != proposed
                    and accepted["intent"] not in ("clarify", "inspect")
                    and not safe_semantic_normalization(proposed, accepted)
                ):
                    paths = semantic_delta_paths(proposed, accepted)
                    raise AdapterError("MODEL_SEMANTICALLY_OVERRIDDEN_FIELDS:" + ",".join(paths))
                trace.append(
                    {
                        "model": model,
                        "provider": "OneAPI",
                        "status": "ACCEPTED",
                        "latency_seconds": round(time.perf_counter() - start, 3),
                        "raw_intent": proposed["intent"],
                        "validated_intent": accepted["intent"],
                        "local_safety_adjustment": accepted != proposed,
                    }
                )
                self.history = list(trace)
                is_real = self.transport.opener is None
                recovered = bool(getattr(a, "last_recovered", False))
                return json.dumps(accepted, ensure_ascii=False), {
                    "source": "RECOVERED_ENCRYPTED_PROVIDER_REPLY"
                    if recovered
                    else ("REAL_MODEL" if is_real else "LOCAL_ADAPTER_FIXTURE"),
                    "recovered_from_durable_response": recovered,
                    "_response_handle": getattr(a, "last_response_handle", None),
                    "provider": "OneAPI",
                    "model": model,
                    "reported_model": model,
                    "requested_model": model,
                    "usage": a.last_usage,
                    "model_calls": max(0, len(self.transport.attempts()) - before_calls)
                    if is_real and not recovered and hasattr(self.transport, "attempts")
                    else 0,
                    "attempts": trace,
                    "fallback_attempts": len(trace) - 1,
                    "automatic_retries": 0,
                    "routing_policy": policy.route,
                    "policy_reason": policy.reason,
                    "validated_by": "TRAVEL_DETERMINISTIC_GATE",
                }
            except (AdapterError, ValueError, TypeError, KeyError) as e:
                reason = str(e)
                trace.append(
                    {
                        "model": model,
                        "provider": "OneAPI",
                        "status": "REJECTED",
                        "reason": reason[:90],
                        "latency_seconds": round(time.perf_counter() - start, 3),
                    }
                )
                if reason.startswith("RESPONSE_CACHE_") or reason in (
                    "LIVE_TEST_DISABLED",
                    "FREE_BUDGET_EXHAUSTED",
                    "PREVIOUS_IDENTICAL_OUTCOME_UNKNOWN",
                    "TOTAL_REQUEST_LIMIT_REACHED",
                    "FREE_CREDIT_NOT_CURRENTLY_PROVEN",
                    "HTTP_401",
                    "HTTP_402",
                    "HTTP_403",
                ):
                    self.history = list(trace)
                    raise AdapterError(reason) from None
                if reason in ("HTTP_429", "PROVIDER_RATE_LIMIT_COOLDOWN"):
                    # Same provider rate limit: no second request to that API.
                    # Deterministic local-only recovery below is independently checked.
                    break
        # Fail closed: if every LLM result is unusable, a fully deterministic
        # *independently validated* operation may still be proposed.
        # Never present it as a successful model response or confirmed supplier quote.
        try:
            deterministic = local_rules(text, context)
            accepted, _, _ = validate_intent(deterministic, text, context)
            if accepted != deterministic:
                raise AdapterError("LOCAL_FALLBACK_NEEDS_REVIEW")
            kind = accepted.get("intent")
            if kind not in ("search", "budget", "constraints", "refine", "inspect", "clarify"):
                raise AdapterError("LOCAL_FALLBACK_INTENT_NOT_ALLOWED")
            if kind in ("search", "budget", "constraints", "refine"):
                if accepted.get("needs") or not accepted.get("values"):
                    raise AdapterError("LOCAL_FALLBACK_MISSING_REQUIRED_FIELDS")
            elif accepted.get("values"):
                raise AdapterError("LOCAL_NONMUTATING_FIELDS_FORBIDDEN")
            trace.append(
                {
                    "provider": "LOCAL",
                    "model": None,
                    "status": "DETERMINISTIC_ACCEPTED",
                    "reason": "ALL_MODELS_UNUSABLE_INDEPENDENTLY_VALIDATED",
                }
            )
            self.history = list(trace)
            calls = (
                len(self.transport.attempts()) - before_calls
                if hasattr(self.transport, "attempts") and self.transport.opener is None
                else 0
            )
            return json.dumps(accepted, ensure_ascii=False), {
                "source": "LOCAL_RULES_AFTER_UNUSABLE_LLM",
                "provider": "LOCAL",
                "model": None,
                "requested_model": None,
                "reported_model": None,
                "model_calls": max(0, calls),
                "fallback_attempts": len(trace) - 1,
                "attempts": trace,
                "automatic_retries": 0,
                "routing_policy": "LOCAL_DETERMINISTIC_RECOVERY",
                "policy_reason": "MODEL_INVALID_OR_UNAVAILABLE",
                "validated_by": "TRAVEL_DETERMINISTIC_GATE",
                "model_answer_accepted": False,
                "hotel_availability_verified": False,
                "monetary_cost": "MODEL_CALLS_MAY_HAVE_OCCURRED_SEE_LEDGER",
            }
        except (AdapterError, ValueError, TypeError, KeyError):
            self.history = list(trace)
            raise AdapterError("ALL_ONEAPI_ROUTES_FAILED_SAFE_NO_STATE_CHANGE") from None

    def acknowledge_application_result(self, identifier):
        f = getattr(self.transport, "acknowledge_application_result", None)
        return bool(f(identifier)) if callable(f) else False

    def status(self):
        return {
            "mode": "ONEAPI_VALIDATED_SHADOW",
            "real_enabled": False,
            "routes": [{"provider": p, "model": m} for p, m in self.routes],
            "reformboss_enabled": False,
            "automatic_retries": 0,
            "trace": self.history,
        }
