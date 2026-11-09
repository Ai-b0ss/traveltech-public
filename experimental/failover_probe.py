"""OneAPI-only failover lab: simulated GLM outage + one actual MiMo inference.

No Reformboss calls. No GLM inference for the injected outage case.
Always bound to the exact fixed synthetic prompt, no arbitrary text.
"""

import json
from pathlib import Path
from ai_router import AdapterError
from oneapi_validated_router import OneAPIValidatedRouter

PROMPT = "Подбери отель в Сочи с 6 по 9 ноября 2026 года для 2 взрослых в 1 номере, бюджет до 34 тысяч рублей. Нужен завтрак."
PRIMARY = "glm-5.3-flash"
SECONDARY = "mimo-v2.6-flash"
CHAT_ENDPOINT = "https://api.oneapi.id/v1/chat/completions"


def is_probe_payload(payload):
    try:
        return (
            payload["messages"][-1]["role"] == "user"
            and json.loads(payload["messages"][-1]["content"])["user_text"] == PROMPT
        )
    except (TypeError, KeyError, ValueError, IndexError, AttributeError):
        return False


class OneAPIProbeRouter(OneAPIValidatedRouter):
    def __init__(self, transport):
        super().__init__(transport, routes=[("OneAPI", PRIMARY), ("OneAPI", SECONDARY)])

    def status(self):
        calls = self.transport.attempts()
        return {
            "mode": "LIVE_SYNTHETIC_TEST",
            "real_enabled": self.transport.opener is None,
            "routes": [
                {"provider": "OneAPI", "model": PRIMARY, "role": "INJECTED_UNAVAILABLE"},
                {"provider": "OneAPI", "model": SECONDARY, "role": "REAL_FALLBACK"},
            ],
            "routing": "GLM_SIMULATED_503_THEN_ONEAPI_MIMO_REAL",
            "automatic_retries": 0,
            "reformboss_enabled": False,
            "remaining_requests": max(0, 1 - len(calls)),
            "attempt_count": len(calls),
            "last_transport_status": calls[-1]["status"] if calls else "NOT_ATTEMPTED",
            "trace": self.history,
        }


class ProbeTransport:
    """Wrap GatewayTransport. No simulated primary reservations or network."""

    def __init__(self, backing, fresh_credit_check):
        self.backing = backing
        self.fresh_credit_check = fresh_credit_check
        self.grant = backing.grant
        # Preserve real-versus-fixture attribution from the underlying gateway.
        self.opener = backing.opener
        self.injected_events = []

    def attempts(self):
        return self.backing.attempts()

    def close(self):
        return self.backing.close()

    def __call__(self, endpoint, payload):
        if endpoint != CHAT_ENDPOINT or not is_probe_payload(payload):
            raise AdapterError("ONLY_FIXED_SYNTHETIC_PROMPT_AUTHORIZED")
        model = payload.get("model")
        if model == PRIMARY:
            self.injected_events.append({"model": model, "event": "INJECTED_503_NO_NETWORK"})
            raise AdapterError("INJECTED_HTTP_503_NO_DISPATCH")
        if model != SECONDARY:
            raise AdapterError("UNAUTHORIZED_MODEL_ROUTE")
        if len(self.attempts()) >= 1:
            raise AdapterError("TOTAL_REQUEST_LIMIT_REACHED")
        self.fresh_credit_check(SECONDARY)
        return self.backing(endpoint, payload)
