"""Replay an actual saved OneAPI MiMo success without making any model call.

Input, provider identity and answer are read from the append-only historical benchmark.
This is NOT live inference; receipt labels it unambiguously.
"""

import json
from pathlib import Path
from ai_router import AdapterError
from natural_language import validate_intent
from failover_probe import SECONDARY


class RecordedMimoReplayClient:
    def __init__(self, rows_path):
        target = "future_search_breakfast"
        path = Path(rows_path)
        if not path.is_file():
            raise AdapterError("RECORDED_EVIDENCE_NOT_FOUND")
        matches = {}
        for line in path.read_text(encoding="utf8").splitlines():
            row = json.loads(line)
            if (
                row.get("case") == target
                and row.get("model_requested") == SECONDARY
                and row.get("status") == "RESPONSE_RECEIVED"
                and isinstance(row.get("response_obj"), dict)
            ):
                matches["found"] = row
        if "found" not in matches:
            raise AdapterError("RECORDED_MIMO_COMPLETE_RESPONSE_MISSING")
        obj = matches["found"]["response_obj"]
        if (
            obj.get("model") != SECONDARY
            or len(obj.get("choices", [])) != 1
            or obj["choices"][0].get("finish_reason") != "stop"
        ):
            raise AdapterError("RECORDED_MIMO_IDENTITY_OR_FINISH_INVALID")
        self.answer = obj["choices"][0]["message"]["content"]
        self.text = "Подбери отель в Сочи с 6 по 9 ноября 2026 года для 2 взрослых в 1 номере, бюджет до 34 тысяч рублей. Нужен завтрак."
        self.case = target
        self.usage = {
            k: v
            for k, v in obj.get("usage", {}).items()
            if k in ("prompt_tokens", "completion_tokens", "total_tokens")
        }

    def infer(self, messages):
        parsed = json.loads(messages[-1]["content"])
        if parsed.get("user_text") != self.text or parsed.get("current_request") is not None:
            raise AdapterError("RECORDED_REPLAY_EXACT_CASE_ONLY")
        validated, _, _ = validate_intent(self.answer, self.text, None)
        return json.dumps(validated, ensure_ascii=False), {
            "source": "RECORDED_REAL_MIMO_OFFLINE_REPLAY",
            "provider": "OneAPI",
            "model": SECONDARY,
            "requested_model": SECONDARY,
            "reported_model": SECONDARY,
            "historical_case": self.case,
            "model_calls": 0,
            "automatic_retries": 0,
            "usage": self.usage,
            "monetary_cost": "NO_NEW_MODEL_CALL",
            "validated_by": "TRAVEL_DETERMINISTIC_GATE",
            "attempts": [
                {
                    "provider": "OneAPI",
                    "model": "glm-5.3-flash",
                    "status": "SIMULATED_503_NO_NETWORK",
                },
                {"provider": "OneAPI", "model": SECONDARY, "status": "ARCHIVED_200_REPLAY"},
            ],
            "fallback_attempts": 1,
        }
