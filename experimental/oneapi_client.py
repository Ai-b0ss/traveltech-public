"""Primary MiMo client; zero new live allowance, preserved comparison budget, no fallback."""

from pathlib import Path
import json, time
from model_comparison import Client, CompareError
from reformboss_client import TrialBudget, ModelError
from secret_reader import read_key


class OneAPIClient:
    def __init__(self, config, budget_path, transport=None, key_path=None):
        self.config = Path(config)
        self.transport = transport
        self.key_path = key_path or Path.home() / ".traveltech-secrets/oneapi.key"
        self.settings = json.loads(self.config.read_text(encoding="utf-8"))
        if (
            self.settings.get("model") != "mimo-v2.6-flash"
            or self.settings.get("provider") != "OneAPI"
        ):
            raise ModelError("ONEAPI_PRIMARY_CONFIGURATION_INVALID")
        grant = self.settings.get("authorization")
        fixture = transport is not None and grant == "LOCAL_FIXTURE_NOT_REAL_GRANT"
        self.budget = TrialBudget(budget_path, 3 if fixture else 0, grant or "NO_NEW_ONEAPI_GRANT")
        self.max_output_tokens = int(self.settings.get("max_output_tokens", 512))

    def status(self):
        trial = self.budget.status()
        reason = "ONEAPI_NEW_MODEL_CALLS_NOT_AUTHORIZED" if trial["total_limit"] == 0 else None
        try:
            read_key(self.key_path)
        except (OSError, ValueError, UnicodeError):
            reason = reason or "ONEAPI_KEY_MISSING_OR_INVALID"
        if self.settings["model"] not in self.settings.get("available_model_ids", []):
            reason = reason or "ONEAPI_MODEL_NOT_VERIFIED"
        if trial["blocked_unknown"]:
            reason = "MODEL_PREVIOUS_OUTCOME_UNKNOWN"
        elif trial["total_limit"] and not trial["remaining"]:
            reason = "MODEL_TRIAL_LIMIT_REACHED"
        return {
            "provider": "OneAPI",
            "model": self.settings["model"],
            "backup_model": self.settings["backup"]["model"],
            "automatic_fallback": False,
            "available": reason is None,
            "reason": reason,
            "last_error": None,
            "trial": trial,
            "additional_limit": trial["total_limit"],
            "historical_comparison_calls": 30,
            "max_output_tokens": self.max_output_tokens,
            "current_supplier_calls": 0,
            "live_allowance_source": "NO_NEW_ALLOWANCE; PRIOR_30_COMPARISON_CALLS_PRESERVED",
        }

    def _request(self, path, method, body=None, key=None):
        if path not in ("/me", "/models", "/chat/completions"):
            raise ModelError("ONEAPI_ENDPOINT_NOT_ALLOWED")
        if self.transport:
            return self.transport(path, method, body, key or read_key(self.key_path))
        try:
            data, elapsed, code = Client().call(
                "/v1" + path, method, json.loads(body) if body else None
            )
        except CompareError as e:
            raise ModelError(str(e)) from None
        return json.dumps(data, ensure_ascii=False).encode()

    def discover(self):
        data = json.loads(self._request("/models", "GET"))
        ids = [x.get("id") for x in data.get("data", []) if isinstance(x, dict)]
        self.settings["available_model_ids"] = [
            x for x in (self.settings["model"], self.settings["backup"]["model"]) if x in ids
        ]
        self.config.write_text(
            json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if self.settings["model"] not in ids:
            raise ModelError("ONEAPI_PRIMARY_MODEL_NOT_LISTED")
        return {
            "selected": self.settings["model"],
            "backup_listed": self.settings["backup"]["model"] in ids,
            "automatic_fallback": False,
            "models_count": len(ids),
            "source": "AUTHENTICATED_ONEAPI_MODELS_GET",
        }

    def infer(self, messages):
        if self.budget.limit == 0:
            raise ModelError("ONEAPI_NEW_MODEL_CALLS_NOT_AUTHORIZED")
        if self.settings["model"] not in self.settings.get("available_model_ids", []):
            raise ModelError("ONEAPI_MODEL_NOT_VERIFIED")
        key = read_key(self.key_path)
        body = json.dumps(
            {
                "model": self.settings["model"],
                "messages": messages,
                "max_tokens": self.max_output_tokens,
                "temperature": 0,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode()
        if key.encode() in body:
            raise ModelError("MODEL_SECRET_IN_INPUT_BLOCKED")
        identifier = self.budget.reserve(self.settings["model"], body)
        started = time.perf_counter()
        try:
            raw = self._request("/chat/completions", "POST", body, key)
            if key.encode() in raw:
                raise ModelError("MODEL_CREDENTIAL_ECHO_BLOCKED")
            data = json.loads(raw)
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ModelError("MODEL_RESPONSE_INCOMPLETE")
            if data.get("model") != self.settings["model"]:
                raise ModelError("ONEAPI_RESPONSE_MODEL_MISMATCH")
            content = choice.get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ModelError("MODEL_CONTENT_MISSING")
            usage = {
                k: v
                for k, v in data.get("usage", {}).items()
                if k in ("prompt_tokens", "completion_tokens", "total_tokens")
                and type(v) is int
                and v >= 0
            }
            if usage.get("completion_tokens", 0) > self.max_output_tokens:
                raise ModelError("MODEL_OUTPUT_LIMIT_VIOLATION")
            self.budget.finish(identifier, "RESPONSE_RECEIVED", usage)
            return content, {
                "source": "TEST_TRANSPORT" if self.transport else "REAL_MODEL",
                "provider": "OneAPI",
                "model": self.settings["model"],
                "attempt_id": identifier,
                "usage": usage,
                "duration_seconds": time.perf_counter() - started,
                "max_output_tokens": self.max_output_tokens,
                "automatic_retries": 0,
                "monetary_cost": "NOT_REPORTED_BY_SERVICE",
            }
        except Exception as e:
            safe = str(e) if isinstance(e, ModelError) else "MODEL_RESPONSE_FORMAT_INVALID"
            self.budget.finish(
                identifier,
                "UNKNOWN_OUTCOME" if "OUTCOME_UNKNOWN" in safe else "RESPONSE_REJECTED",
                error_code=safe,
            )
            raise ModelError(safe) from None
