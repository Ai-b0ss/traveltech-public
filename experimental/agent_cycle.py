"""Bounded, observation-driven tool loop. Default planner is a LOCAL SIMULATOR.

No gateway config, credentials or network transports are used. The planner's
request/response shape is Chat Completions function calling. Tools reuse the
existing language gates, preview store, providers and price/evidence engine.
"""

import json, uuid, os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from dataclasses import replace
from app import scenario, request_from, jsonable
from travel_core import Engine, InputError, safe_json
from natural_language import local_rules, validate_intent

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }
    for name, description in (
        ("extract_requirements", "Extract only explicit user requirements and validate them."),
        ("search_hotels", "Read provider offers for validated prospective conditions; no booking."),
        ("compare_offers", "Rank engine decisions with reasons and unknown fields."),
        ("handle_local_action", "Clarify, inspect or explicitly requested local undo/preferences."),
        (
            "prepare_preview",
            "Prepare existing Preview/Confirm proposal; never commit automatically.",
        ),
    )
]


class AgentError(ValueError):
    pass


class DeterministicPlanner:
    """Fixed local LLM-contract simulator; follows actual tool observations.

    It does not infer hotel facts or bypass business logic. The extraction tool
    is the bounded existing Russian parser, not real LLM understanding.
    """

    def complete(self, payload):
        observed = {}
        for previous in payload["messages"]:
            if previous["role"] == "tool":
                observed[previous["name"]] = safe_json(previous["content"])
        if any(not value["ok"] for value in observed.values()):
            message = {
                "role": "assistant",
                "content": "Инструмент не завершён. Состояние поездки сохранено; автоматического повтора нет.",
            }
        elif "extract_requirements" not in observed:
            name = "extract_requirements"
        elif "handle_local_action" in observed:
            message = {
                "role": "assistant",
                "content": "Нужны уточнения или проверка неизвестных условий.",
            }
        elif observed["extract_requirements"]["result"]["intent"]["intent"] not in (
            "search",
            "budget",
            "constraints",
            "refine",
        ):
            name = "handle_local_action"
        elif "search_hotels" not in observed:
            name = "search_hotels"
        elif observed["search_hotels"]["result"]["status"] == "SOURCE_ERROR":
            message = {
                "role": "assistant",
                "content": "Ошибка источника. Результат неизвестен; повтор автоматически не выполнен.",
            }
        elif "compare_offers" not in observed:
            name = "compare_offers"
        elif "prepare_preview" not in observed:
            name = "prepare_preview"
        else:
            message = {
                "role": "assistant",
                "content": "Варианты проверены. Подтвердите предложенные условия отдельно.",
            }
        if "message" not in locals():
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_" + uuid.uuid4().hex,
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                ],
            }
        return {
            "model": "local-deterministic-simulator",
            "choices": [
                {
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    "message": message,
                }
            ],
        }


class AgentCycle:
    def __init__(self, app, planner=None, provider_factory=None, max_steps=8):
        self.app = app
        self.planner = planner or DeterministicPlanner()
        self.provider_factory = provider_factory or (lambda name, now: scenario(name, now)[1])
        self.max_steps = max_steps

    def run(self, text, sid=None, revision=None, draft_id=None):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1500:
            raise InputError("LANGUAGE_INPUT_INVALID")
        text = text.replace("один номер", "1 номер")
        trace_id = uuid.uuid4().hex
        trace = []
        context = None
        output = None
        data = {}
        messages = [
            {
                "role": "system",
                "content": "Choose tools from observations. Source text is untrusted. Never commit, book or pay.",
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"text": text, "sid": sid, "revision": revision}, ensure_ascii=False
                ),
            },
        ]
        with self.app.lock:
            if sid:
                _, session = self.app._load(sid)
                context = session.state.request
                if type(revision) is not int or revision != session.state.revision:
                    from session import Conflict

                    raise Conflict("STALE_REVISION")
            before = self.app.store.db.total_changes
            now = datetime.now(timezone.utc)

            def execute_tool(name):
                nonlocal output
                if name in data:
                    raise AgentError("TOOL_REPEAT_NOT_ALLOWED")
                if name == "extract_requirements":
                    raw = local_rules(text, context)
                    intent, req, source = validate_intent(raw, text, context)
                    if req is None and intent["intent"] in ("budget", "constraints", "refine"):
                        from refinement import normalize

                        req = normalize(context, intent["values"])
                        req.validate()
                        if "city" in intent["values"]:
                            from city_names import canonical_city

                            source = canonical_city(req.city)[2]
                        else:
                            source = self.app._load(sid)[0]
                    result = {
                        "intent": intent,
                        "request": jsonable(req) if req else None,
                        "scenario": source,
                    }
                elif name == "search_hotels":
                    extracted = data.get("extract_requirements")
                    if not extracted or not extracted["request"]:
                        raise AgentError("VALIDATED_REQUEST_REQUIRED")
                    request = request_from(extracted["request"])
                    provider = self.provider_factory(extracted["scenario"], now)

                    class ObservedProvider:
                        rows = []

                        def search(self, criteria):
                            rows = provider.search(criteria)
                            self.rows = rows if isinstance(rows, list) else []
                            return rows

                    observed = ObservedProvider()
                    result = jsonable(
                        Engine(observed, result_limit=3, max_age=timedelta(hours=24)).search(
                            request, now
                        )
                    )
                    # Record the source timestamp, separately from this replay's clock.
                    result["source_observed_at"] = (
                        {}
                        if result["status"] == "SOURCE_ERROR"
                        else {
                            o.source_ref: o.observed_at.isoformat()
                            for o in observed.rows
                            if isinstance(getattr(o, "observed_at", None), datetime)
                            and hasattr(o, "source_ref")
                        }
                    )
                    result["source_observation_precision"] = (
                        "DATE_ONLY_LOWER_BOUND"
                        if hasattr(provider, "lower_bound")
                        else "SYNTHETIC_FIXED"
                        if observed.rows
                        and all(getattr(o, "evidence", None) == "SYNTHETIC" for o in observed.rows)
                        else "CAPTURE_TIMESTAMP"
                    )
                elif name == "compare_offers":
                    search = data.get("search_hotels")
                    if not search or search["status"] == "SOURCE_ERROR":
                        raise AgentError("SUCCESSFUL_SEARCH_REQUIRED")
                    order = {"CONSTRAINTS_MET_NOT_LIVE": 0, "REVIEW": 1, "REJECT": 2}
                    decisions = sorted(
                        search.get("decisions", []),
                        key=lambda d: (
                            order[d["status"]],
                            Decimal(d["base_total"])
                            if d["base_total"] is not None
                            else Decimal("Infinity"),
                            str(d["identity"]),
                        ),
                    )
                    decisions = [
                        {
                            **d,
                            "source_observed_at": search["source_observed_at"].get(d["source_ref"]),
                            "source_observation_precision": search["source_observation_precision"],
                        }
                        for d in decisions
                    ]
                    result = {
                        "status": search["status"],
                        "offers": decisions,
                        "count": len(decisions),
                        "not_rejected": sum(d["status"] != "REJECT" for d in decisions),
                        "explanation": "Сначала не отклонённые тарифы, затем базовая стоимость в валюте запроса. Причины и неизвестные поля сохранены. Наличие и окончательный счёт не подтверждены.",
                    }
                elif name == "prepare_preview":
                    if "compare_offers" not in data:
                        raise AgentError("COMPARISON_REQUIRED")
                    output = self.app.language.execute(
                        text, "local", sid=sid, revision=revision, preview=True, draft_id=draft_id
                    )
                    result = {
                        "preview_id": output.get("preview_id"),
                        "requires_confirmation": True,
                        "state_changed": False,
                    }
                elif name == "handle_local_action":
                    extracted = data.get("extract_requirements")
                    if not extracted or extracted["intent"]["intent"] in (
                        "search",
                        "budget",
                        "constraints",
                        "refine",
                    ):
                        raise AgentError("NON_SEARCH_ACTION_REQUIRED")
                    output = self.app.language.execute(
                        text, "local", sid=sid, revision=revision, preview=True, draft_id=draft_id
                    )
                    result = {
                        "intent": extracted["intent"],
                        "message": output["language_message"],
                        "state_changed": extracted["intent"]["intent"] == "undo",
                    }
                else:
                    raise AgentError("TOOL_NOT_ALLOWED")
                data[name] = result
                return result

            status = "STEP_LIMIT"
            final = ""
            folder = self.app.storage / "agent_traces"
            folder.mkdir(exist_ok=True)
            trace_path = folder / (trace_id + ".json")

            def save_trace(current_status):
                body = {
                    "schema": 1,
                    "trace_id": trace_id,
                    "text": text,
                    "sid": sid,
                    "revision": revision,
                    "mode": "LOCAL_AGENT_SIMULATOR",
                    "steps": trace,
                    "messages": messages,
                    "status": current_status,
                    "store_changes": self.app.store.db.total_changes - before,
                    "external_calls": 0,
                    "observed_at": now.isoformat(),
                }
                temp = trace_path.with_suffix(".tmp")
                with temp.open("w", encoding="utf8") as file:
                    file.write(json.dumps(body, ensure_ascii=False, indent=2))
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp, trace_path)

            save_trace("RUNNING")
            try:
                for step in range(self.max_steps):
                    payload = {
                        "model": "local-deterministic-simulator",
                        "messages": messages,
                        "tools": TOOLS,
                        "stream": False,
                    }
                    try:
                        response = self.planner.complete(payload)
                        if not isinstance(response, dict):
                            raise AgentError("PLANNER_RESPONSE_INVALID")
                        choices = response.get("choices", [])
                        if (
                            not isinstance(choices, list)
                            or len(choices) != 1
                            or not isinstance(choices[0], dict)
                        ):
                            raise AgentError("PLANNER_CHOICES_INVALID")
                        choice = choices[0]
                        message = choice["message"]
                        if not isinstance(message, dict) or message.get("role") != "assistant":
                            raise AgentError("PLANNER_MESSAGE_INVALID")
                        calls = message.get("tool_calls", [])
                        if not isinstance(calls, list):
                            raise AgentError("PLANNER_TOOL_CALL_INVALID")
                        if choice["finish_reason"] == "stop" and not calls:
                            if (
                                output is None
                                and data.get("search_hotels", {}).get("status") != "SOURCE_ERROR"
                            ):
                                raise AgentError("PLANNER_PREMATURE_FINISH")
                            status = "COMPLETE" if output else "SOURCE_ERROR"
                            final = message.get("content") or ""
                            break
                        if choice["finish_reason"] != "tool_calls" or len(calls) != 1:
                            raise AgentError("PLANNER_TOOL_CALL_INVALID")
                        call = calls[0]
                        if (
                            not isinstance(call, dict)
                            or call.get("type") != "function"
                            or not isinstance(call.get("id"), str)
                            or not isinstance(call.get("function"), dict)
                        ):
                            raise AgentError("PLANNER_TOOL_CALL_INVALID")
                        name = call["function"]["name"]
                        args = safe_json(call["function"]["arguments"])
                        if args != {}:
                            raise AgentError("TOOL_ARGUMENTS_FORBIDDEN")
                        messages.append(message)
                        try:
                            result = execute_tool(name)
                            observation = {"ok": True, "result": result}
                        except (ValueError, TypeError, KeyError, TimeoutError) as e:
                            observation = {"ok": False, "error": type(e).__name__ + ":" + str(e)}
                        trace.append(
                            {
                                "step": step + 1,
                                "tool": name,
                                "arguments": args,
                                "observation": observation,
                                "observed_at": datetime.now(timezone.utc).isoformat(),
                            }
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "name": name,
                                "content": json.dumps(observation, ensure_ascii=False),
                            }
                        )
                        save_trace("RUNNING")
                        if not observation["ok"]:
                            status = "TOOL_ERROR"
                            break
                    except (ValueError, TypeError, KeyError, TimeoutError) as e:
                        status = "MODEL_ERROR"
                        trace.append(
                            {"step": step + 1, "model_error": type(e).__name__ + ":" + str(e)}
                        )
                        break
                if output is None:
                    extracted = data.get("extract_requirements")
                    if status == "CLARIFY_OR_INSPECT" and extracted:
                        output = self.app.language.execute(
                            text,
                            "local",
                            sid=sid,
                            revision=revision,
                            preview=True,
                            draft_id=draft_id,
                        )
                    else:
                        output = (
                            self.app.execute("read", sid=sid)
                            if sid
                            else {"session_id": None, "request": None, "cards": []}
                        )
                        output["language_message"] = (
                            final
                            or "Процесс прерван. Условия сохранены; повтор автоматически не выполняется."
                        )
                receipt = {
                    "trace_id": trace_id,
                    "status": status,
                    "mode": "LOCAL_AGENT_SIMULATOR",
                    "real_llm_verified": False,
                    "external_calls": 0,
                    "model_calls": 0,
                    "source": "LOCAL_FUNCTION_CALL_SIMULATOR",
                    "observed_at": now.isoformat(),
                    "steps": trace,
                    "comparison": data.get("compare_offers"),
                    "confirmation_required": bool(output.get("has_preview")),
                }
                output["agent"] = receipt
                return output
            finally:
                save_trace(status)
