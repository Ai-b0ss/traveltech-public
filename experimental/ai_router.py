"""Offline/shadow router. Fixtures exercise real adapter gates; no network by default."""

import json, time, re, threading
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from travel_core import InputError, safe_json


class AdapterError(ValueError):
    pass


class ModelRegistry:
    def __init__(self, path=None):
        path = path or Path(__file__).resolve().parents[1] / "model_registry.json"
        self.data = json.loads(Path(path).read_text(encoding="utf-8"))
        if self.data.get("schema") != 1 or self.data.get("default_mode") != "OFFLINE_DEMO":
            raise AdapterError("REGISTRY_SCHEMA_INVALID")
        self.routes = self.data["routes"]

    def route(self, provider, model):
        result = next(
            (r for r in self.routes if r["provider"] == provider and r["model_id"] == model), None
        )
        if result is None:
            raise AdapterError("ROUTE_NOT_REGISTERED")
        return result

    def candidates(self, task, api_type="CHAT_COMPLETIONS"):
        return [
            r
            for r in self.routes
            if task in r["tasks"] and r["api_type"] == api_type and r.get("shadow_eligible")
        ]


class OfflineClient:
    def infer(self, *args, **kwargs):
        raise AdapterError("LIVE_TEST_DISABLED")

    def status(self):
        return {
            "mode": "OFFLINE_DEMO",
            "real_enabled": False,
            "external_calls": 0,
            "cost_usd": "0",
            "model_registry": "model_registry.json",
            "live_status": "NOT_PROVEN_THIS_BUILD",
        }


class ChatAdapter:
    def __init__(self, provider, model, transport=None):
        self.provider = provider
        self.model = model
        self.transport = transport
        # Do not share request receipts between parallel calls on one adapter.
        self._call_meta = threading.local()
        self.endpoint = {
            "OneAPI": "https://api.oneapi.id/v1/chat/completions",
            "Reformboss": "https://api.reformboss.com/v1/chat/completions",
        }[provider]

    @property
    def last_response_handle(self):
        return getattr(self._call_meta, "response_handle", None)

    @last_response_handle.setter
    def last_response_handle(self, value):
        self._call_meta.response_handle = value

    @property
    def last_recovered(self):
        return getattr(self._call_meta, "recovered", False)

    @last_recovered.setter
    def last_recovered(self, value):
        self._call_meta.recovered = bool(value)

    @property
    def last_usage(self):
        return getattr(self._call_meta, "usage", {})

    @last_usage.setter
    def last_usage(self, value):
        self._call_meta.usage = value

    def payload(self, messages):
        if not isinstance(messages, list) or not messages:
            raise AdapterError("MESSAGES_REQUIRED")
        # Only fields guaranteed across gateway conversions. JSON is validated after response.
        return {"model": self.model, "messages": messages, "max_tokens": 2048, "stream": False}

    def decode(self, response):
        # Provider replies are untrusted: reject bad envelopes using the same
        # bounded AdapterError contract as truncated or invalid JSON replies.
        # Never let AttributeError bypass independent route failover.
        if type(response) is not dict:
            raise AdapterError("MODEL_ENVELOPE_INVALID")
        if response.get("model") != self.model:
            raise AdapterError("REPORTED_MODEL_MISMATCH")
        choices = response.get("choices")
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            raise AdapterError("MODEL_CHOICES_INVALID")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise AdapterError("INCOMPLETE_FINISH")
        message = choice.get("message")
        if (
            type(message) is not dict
            or type(message.get("content")) is not str
            or not message["content"].strip()
        ):
            raise AdapterError("MODEL_CONTENT_INVALID")
        value = safe_json(message["content"])
        if (
            not isinstance(value, dict)
            or set(value) != {"intent", "values", "needs"}
            or value["intent"]
            not in (
                "search",
                "budget",
                "constraints",
                "refine",
                "inspect",
                "clarify",
                "undo",
                "preference",
            )
            or not isinstance(value["values"], dict)
            or not isinstance(value["needs"], list)
        ):
            raise AdapterError("INVALID_INTENT_SCHEMA")
        usage = response.get("usage", {})
        if type(usage) is not dict:
            raise AdapterError("MODEL_USAGE_INVALID")
        self.last_usage = {
            k: v
            for k, v in usage.items()
            if k in ("prompt_tokens", "completion_tokens", "total_tokens")
            and type(v) is int
            and v >= 0
        }
        if self.last_usage.get("completion_tokens", 0) > 2048:
            raise AdapterError("OUTPUT_LIMIT_VIOLATION")
        return value

    def infer(self, messages):
        if self.transport is None:
            raise AdapterError("LIVE_TEST_DISABLED")
        result = self.transport(self.endpoint, self.payload(messages))
        self.last_recovered = bool(getattr(self.transport, "last_was_recovered", False))
        self.last_response_handle = getattr(self.transport, "last_response_handle", None)
        return self.decode(result)


class LanguageModelClient:
    """Bridge the shared Chat adapter to NaturalLanguage's (raw, receipt) contract."""

    def __init__(self, adapter):
        self.adapter = adapter

    def infer(self, messages):
        result = self.adapter.infer(messages)
        from gateway_transport import GatewayTransport

        real = (
            isinstance(self.adapter.transport, GatewayTransport)
            and self.adapter.transport.opener is None
        )
        recovered = bool(getattr(self.adapter, "last_recovered", False))
        source = (
            "RECOVERED_ENCRYPTED_PROVIDER_REPLY"
            if recovered
            else ("REAL_MODEL" if real else "LOCAL_ADAPTER_FIXTURE")
        )
        return json.dumps(result, ensure_ascii=False), {
            "source": source,
            "provider": self.adapter.provider,
            "model": self.adapter.model,
            "requested_model": self.adapter.model,
            "reported_model": self.adapter.model,
            "usage": getattr(self.adapter, "last_usage", {}),
            "model_calls": 1 if real and not recovered else 0,
            "automatic_retries": 0,
            "recovered_from_durable_response": recovered,
            "_response_handle": getattr(self.adapter, "last_response_handle", None),
            "monetary_cost": "PREVIOUS_REQUEST_ALREADY_ACCOUNTED"
            if recovered
            else ("NOT_REPORTED_BY_SERVICE" if real else "NO_MODEL_CALL"),
        }

    def acknowledge_application_result(self, identifier):
        f = getattr(self.adapter.transport, "acknowledge_application_result", None)
        return bool(f(identifier)) if callable(f) else False

    def status(self):
        return {
            "provider": self.adapter.provider,
            "model": self.adapter.model,
            "live_enabled": self.adapter.transport is not None,
            "connectivity": "NOT_PROVEN_BY_CONFIGURATION",
        }


class JevAdapter:
    endpoint = "https://api.oneapi.id/v1/systemone"
    model = "jev-1.13"
    alternatives = {
        "LOCAL_BUDGET": "Явная арифметическая правка бюджета",
        "CLARIFY": "Не хватает точных условий",
        "GENERAL_PARSE": "Сложный запрос",
        "INSPECT": "Объяснить доказательства",
    }

    def __init__(self, transport=None):
        self.transport = transport

    def infer(self, text):
        if self.transport is None:
            raise AdapterError("LIVE_TEST_DISABLED")
        return self.decode(self.transport(self.endpoint, self.payload(text)))

    def payload(self, text):
        return {
            "model": self.model,
            "state": {"user_text": text},
            "questions": {
                "next_action": {
                    "type": "choice",
                    "instructions": "Выбери только следующее действие; не применяй состояние.",
                    "criteria": self.alternatives,
                }
            },
            "stream": False,
        }

    def decode(self, response):
        if response.get("model") != self.model:
            raise AdapterError("REPORTED_MODEL_MISMATCH")
        if set(response.get("answers", {})) != {"next_action"}:
            raise AdapterError("JEV_SCHEMA_INVALID")
        answer = response["answers"]["next_action"]
        p = answer.get("probabilities", {})
        if (
            answer.get("type") != "choice"
            or answer.get("choice") not in self.alternatives
            or set(p) != set(self.alternatives)
        ):
            raise AdapterError("JEV_CHOICE_INVALID")
        if (
            any(type(x) not in (int, float) or not 0 <= x <= 1 for x in p.values())
            or abs(sum(p.values()) - 1) > 0.0001
        ):
            raise AdapterError("JEV_PROBABILITIES_INVALID")
        confidence = answer.get("confidence")
        if (
            type(confidence) not in (int, float)
            or not 0 <= confidence <= 1
            or p[answer["choice"]] < max(p.values())
        ):
            raise AdapterError("JEV_CHOICE_INVALID")
        return answer


def retry_seconds(value, now):
    try:
        return max(0, min(3600, float(value)))
    except (ValueError, TypeError):
        try:
            return max(0, min(3600, parsedate_to_datetime(value).timestamp() - now))
        except (ValueError, TypeError, OverflowError):
            return 60


class RouteHealth:
    def __init__(self):
        self.disabled = False
        self.cooldown_until = 0
        self.failures = 0
        self.last_error = None

    def failure(self, status, now, retry_after=None):
        self.last_error = str(status)
        self.failures += 1
        if status == 402:
            self.disabled = True
        elif status == 429:
            self.cooldown_until = now + retry_seconds(retry_after, now)
        elif status == 503:
            self.cooldown_until = now + min(300, 30 * self.failures)
        elif status == "TIMEOUT":
            self.cooldown_until = now + 60

    def allowed(self, now):
        return not self.disabled and now >= self.cooldown_until


SCENARIOS = {
    "oneapi_ok": ("OK", "503", "OK"),
    "reformboss_ok": ("429", "OK", "OK"),
    "invalid_json": ("JSON", "OK", "OK"),
    "timeout": ("TIMEOUT", "OK", "OK"),
    "both_down": ("503", "503", "OK"),
    "jev_down": ("OK", "OK", "503"),
    "wrong_model": ("MODEL", "OK", "OK"),
    "payment_required": ("402", "OK", "OK"),
    "truncated": ("LENGTH", "OK", "OK"),
}


class ShadowRouter:
    def __init__(self):
        self.registry = ModelRegistry()
        self.health = {k: RouteHealth() for k in ("OneAPI", "Reformboss")}

    def select_task(self, text):
        if re.search(r"бюджет|процент|вдвое", text, re.I):
            return "LOCAL_DETERMINISTIC"
        if re.search(r"налог|вместим|подтвержден", text, re.I):
            return "LOCAL_EVIDENCE"
        return "GENERAL_PARSE"

    def simulate(self, scenario):
        if scenario not in SCENARIOS:
            raise InputError("UNKNOWN_SHADOW_SCENARIO")
        # Each demo is an isolated experiment; health within each experiment persists.
        health = {k: RouteHealth() for k in ("OneAPI", "Reformboss")}
        self.health = health
        statuses = SCENARIOS[scenario]
        now = time.time()
        steps = []
        selected = "LOCAL_DETERMINISTIC"
        jev = JevAdapter()
        answer = {
            "model": jev.model,
            "answers": {
                "next_action": {
                    "type": "choice",
                    "choice": "GENERAL_PARSE",
                    "confidence": 0.8,
                    "probabilities": {
                        "LOCAL_BUDGET": 0.1,
                        "CLARIFY": 0.05,
                        "GENERAL_PARSE": 0.8,
                        "INSPECT": 0.05,
                    },
                }
            },
        }
        if statuses[2] == "OK":
            jev.decode(answer)
            steps.append(
                {
                    "route": "Jev / System One",
                    "result": "SIMULATED_CLASSIFICATION",
                    "reason": "Ограниченный выбор действия; не извлечение полей",
                }
            )
        else:
            steps.append(
                {
                    "route": "Jev / System One",
                    "result": "SIMULATED_503",
                    "reason": "Классификация пропущена; общий разбор разрешён",
                }
            )
        proposed = {"intent": "clarify", "values": {}, "needs": ["checkin", "checkout"]}
        for provider, model, status in zip(
            ("OneAPI", "Reformboss"), ("mimo-v2.6-flash", "deepseek-v4.1-flash"), statuses[:2]
        ):
            registered = self.registry.route(provider, model)
            if "GENERAL_PARSE" not in registered["tasks"]:
                raise AdapterError("TASK_ROUTE_INCOMPATIBLE")
            adapter = ChatAdapter(provider, model)
            response = {
                "model": model,
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(proposed)}}
                ],
            }
            if status in ("402", "429", "503", "TIMEOUT"):
                code = int(status) if status.isdigit() else status
                health[provider].failure(code, now, "17" if code == 429 else None)
                reason = {
                    "402": "Маршрут отключён для текущего тестового ключа",
                    "429": "Retry-After учтён; маршрут в cooldown",
                    "503": "Circuit breaker открыт; сбой провайдера",
                    "TIMEOUT": "Исход неизвестен. Автоматический повтор запрещён",
                }[status]
                steps.append(
                    {
                        "route": provider + "/" + model,
                        "result": "UNKNOWN_OUTCOME"
                        if status == "TIMEOUT"
                        else "SIMULATED_HTTP_" + status,
                        "reason": reason,
                        "disabled": health[provider].disabled,
                        "cooldown_seconds": round(health[provider].cooldown_until - now),
                    }
                )
                continue
            if status == "JSON":
                response["choices"][0]["message"]["content"] = "{broken"
            if status == "MODEL":
                response["model"] = "other-model"
            if status == "LENGTH":
                response["choices"][0]["finish_reason"] = "length"
            try:
                adapter.decode(response)
                selected = provider + "/" + model
                steps.append(
                    {
                        "route": selected,
                        "result": "SIMULATED_VALID_JSON",
                        "reason": "Структура проверена адаптером; применение состояния запрещено",
                    }
                )
                break
            except (ValueError, KeyError, TypeError) as e:
                steps.append(
                    {"route": provider + "/" + model, "result": "REJECTED", "reason": str(e)}
                )
        if selected == "LOCAL_DETERMINISTIC":
            steps.append(
                {
                    "route": selected,
                    "result": "SAFE_LOCAL_FALLBACK",
                    "reason": "Модельные ответы недоступны; локальный разбор и сессия работают",
                }
            )
        for step in steps:
            if "/" in step["route"] and step["route"].split("/")[0] in ("OneAPI", "Reformboss"):
                step["requested_model"] = step["route"].split("/")[1]
                step["reported_model"] = (
                    "other-model"
                    if step["result"] == "REJECTED" and step["reason"] == "REPORTED_MODEL_MISMATCH"
                    else step["requested_model"]
                    if step["result"] == "SIMULATED_VALID_JSON"
                    else None
                )
        return {
            "mode": "SHADOW",
            "scenario": scenario,
            "selected_route": selected,
            "steps": steps,
            "safe_fallback": len(steps) > 2,
            "external_calls": 0,
            "real_quota_spent": False,
            "actual_cost_usd": "0",
            "estimated_live_cost_usd": None,
            "alternative_state_applied": False,
            "state_preserved": True,
        }
