"""Offline demonstration of real failover policy under two simulated model errors."""

from ai_router import AdapterError
from oneapi_validated_router import OneAPIValidatedRouter
from failover_probe import PROMPT


class SimulatedUnavailableTransport:
    opener = object()

    def __call__(self, endpoint, payload):
        if payload.get("model") == "glm-5.3-flash":
            raise AdapterError("INJECTED_HTTP_503_NO_DISPATCH")
        if payload.get("model") == "mimo-v2.6-flash":
            raise AdapterError("INJECTED_INVALID_JSON_NO_DISPATCH")
        raise AdapterError("UNAUTHORIZED_MODEL")

    def attempts(self):
        return []


class LocalRecoveryDemoClient:
    def __init__(self):
        self.inner = OneAPIValidatedRouter(SimulatedUnavailableTransport())
        self.text = PROMPT

    def infer(self, messages):
        import json

        data = json.loads(messages[-1]["content"])
        if data.get("user_text") != self.text or data.get("current_request") is not None:
            raise AdapterError("EXACT_DEMO_CASE_ONLY")
        return self.inner.infer(messages)

    def status(self):
        return {
            "mode": "OFFLINE_SAFE_RECOVERY",
            "real_enabled": False,
            "routes": [
                {"provider": "OneAPI", "model": "glm-5.3-flash", "role": "SIMULATED_503"},
                {
                    "provider": "OneAPI",
                    "model": "mimo-v2.6-flash",
                    "role": "SIMULATED_INVALID_JSON",
                },
            ],
            "model_calls": 0,
            "reformboss_enabled": False,
            "proof": "LOCAL_RULES_AFTER_UNUSABLE_LLM",
        }
