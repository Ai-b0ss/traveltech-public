"""Primary plus backup chat routes. Only non-mutating language proposals can fail over."""

import json, time
from ai_router import ChatAdapter, AdapterError


class ManagedFallbackClient:
    def __init__(self, routes, transport):
        self.routes = list(routes)
        self.transport = transport
        self.attempt_trace = []
        if not self.routes:
            raise AdapterError("ROUTES_REQUIRED")

    def infer(self, messages):
        attempts = []
        for provider, model in self.routes:
            adapter = ChatAdapter(provider, model, self.transport)
            started = time.perf_counter()
            try:
                proposal = adapter.infer(messages)
                attempts.append(
                    {
                        "provider": provider,
                        "model": model,
                        "status": "COMPLETE",
                        "latency_seconds": round(time.perf_counter() - started, 3),
                    }
                )
                self.attempt_trace = attempts
                recovered = bool(getattr(adapter, "last_recovered", False))
                receipt = {
                    "source": "RECOVERED_ENCRYPTED_PROVIDER_REPLY"
                    if recovered
                    else (
                        "REAL_MODEL" if self.transport.opener is None else "LOCAL_ADAPTER_FIXTURE"
                    ),
                    "recovered_from_durable_response": recovered,
                    "_response_handle": getattr(adapter, "last_response_handle", None),
                    "provider": provider,
                    "model": model,
                    "requested_model": model,
                    "reported_model": model,
                    "model_calls": sum(1 for a in attempts if a.get("status") != "SKIPPED")
                    if self.transport.opener is None and not recovered
                    else 0,
                    "fallback_attempts": len(attempts) - 1,
                    "attempts": attempts,
                    "usage": adapter.last_usage,
                    "automatic_retries": 0,
                    "monetary_cost": "NOT_REPORTED_BY_SERVICE",
                }
                return json.dumps(proposal, ensure_ascii=False), receipt
            except (AdapterError, ValueError, KeyError, TypeError) as exc:
                error = str(exc)
                attempts.append(
                    {
                        "provider": provider,
                        "model": model,
                        "status": "REJECTED",
                        "reason": error[:100],
                        "latency_seconds": round(time.perf_counter() - started, 3),
                    }
                )
                if error.startswith("RESPONSE_CACHE_") or error in (
                    "PREVIOUS_IDENTICAL_OUTCOME_UNKNOWN",
                    "FREE_BUDGET_EXHAUSTED",
                    "LIVE_TEST_DISABLED",
                    "LIVE_BUDGET_INVALID",
                    "HTTP_429",
                    "HTTP_401",
                    "HTTP_402",
                    "HTTP_403",
                    "PROVIDER_RATE_LIMIT_COOLDOWN",
                ):
                    # A repeated/blocked operation must not trigger a bypass via a second route.
                    self.attempt_trace = attempts
                    raise AdapterError(error) from None
        self.attempt_trace = attempts
        raise AdapterError("ALL_MODEL_ROUTES_UNAVAILABLE")

    def acknowledge_application_result(self, identifier):
        f = getattr(self.transport, "acknowledge_application_result", None)
        return bool(f(identifier)) if callable(f) else False

    def status(self):
        attempts = self.transport.attempts() if hasattr(self.transport, "attempts") else []
        grant = getattr(self.transport, "grant", {})
        limit = grant.get("max_total_requests")
        remaining = max(0, limit - len(attempts)) if type(limit) is int else None
        return {
            "mode": "LIVE_SYNTHETIC_TEST",
            "real_enabled": self.transport.opener is None,
            "routing": "primary_then_independent_backup",
            "routes": [{"provider": p, "model": m} for p, m in self.routes],
            "automatic_retries": 0,
            "state_mutated_by_model": False,
            "last_attempt_trace": self.attempt_trace,
            "attempt_count": len(attempts),
            "remaining_requests": remaining,
            "last_transport_status": attempts[-1]["status"] if attempts else "NOT_ATTEMPTED",
        }
