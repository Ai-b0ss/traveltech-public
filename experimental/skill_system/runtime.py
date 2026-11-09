"""Model interpretation -> existing backend operations -> evidence selection -> Preview."""

import request_deadline as deadline
import json, re, time, uuid, hashlib, sqlite3, copy
from pathlib import Path
from app import jsonable
from provider_transport import Failure, canonical
from .contracts import INTENT_SCHEMA, tool, decode, presentation_schema, ContractError, completed
from .registry import Registry
from .backend import Backend, validate_proposal, invalidations
from .jev import JevAdvisor
from .trust_boundary import compose_messages
from natural_language import local_rules


class SkillAgent:
    def __init__(self, app, transport, registry=None, advisor=None):
        self.app = app
        self.transport = transport
        self.registry = registry or Registry()
        self.backend = Backend(app)
        self.advisor = advisor or JevAdvisor()
        with self.app.lock:
            self.app.store.db.execute(
                "CREATE TABLE IF NOT EXISTS skill_runs(id TEXT PRIMARY KEY,state TEXT,record TEXT)"
            )

    def save(self, record):
        with self.app.lock:
            self.app.store.db.execute(
                "INSERT INTO skill_runs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,record=excluded.record",
                (record["id"], record["status"], canonical(record)),
            )

    def checkpoint(self, run):
        # ask() also supports standalone validated operations without a durable run.
        if run.get("status") == "RUNNING":
            self.save(run)

    def progress(self, run, stage):
        deadline.check()
        run["stage"] = stage
        run["stage_started_at"] = time.time()
        self.checkpoint(run)

    def advise(self, state, tag):
        left = deadline.remaining()
        if (
            left is not None
            and left < deadline.CRITICAL_RESERVE_SECONDS + deadline.OPTIONAL_SECONDS
        ):
            return {
                "status": "SKIPPED",
                "reason": "REQUEST_TIME_RESERVED_FOR_MAIN_FLOW",
                "enforced": False,
            }
        with deadline.budget(deadline.OPTIONAL_SECONDS):
            return self.advisor.evaluate(state, self.registry.index(), tag)

    def ask(self, provider, state, names, name, schema, run, semantic=None):
        self.progress(run, "UNDERSTANDING" if name == "propose_intent" else "COMPARING")
        ctx = self.registry.context(names, state)
        if name == "propose_intent" and "input_observations" in state:
            ctx["state"]["input_observations"] = state["input_observations"]
        for p in self.registry.resolve(names):
            run["active_skills"][p["id"]] = {"version": p["version"], "hash": p["hash"]}
        tools = [
            tool(
                name,
                "Верни структурированное решение по загруженной процедуре. Backend исполняет операции отдельно.",
                schema,
            )
        ]
        error = None
        for attempt in range(2):
            deadline.check()
            messages = compose_messages(ctx, error)
            payload_bytes = len(canonical({"messages": messages, "tools": tools}).encode("utf-8"))
            # Measure UTF-8 bytes, not an invented provider token count. Never
            # truncate critical procedure text or an evidence/schema constraint.
            if payload_bytes > 180000:
                raise Failure("SKILL_CONTEXT_BYTE_LIMIT")
            metric = {
                "operation": name,
                "attempt": attempt,
                "payload_bytes": payload_bytes,
                "context_bytes": len(canonical(ctx).encode("utf-8")),
                "instruction_bytes": sum(
                    len(p["instructions"].encode("utf-8"))
                    + sum(len(v.encode("utf-8")) for v in p["resource_texts"].values())
                    for p in self.registry.resolve(names)
                ),
                "limit_bytes": 180000,
                "skill_ids": [p["id"] for p in self.registry.resolve(names)],
            }
            run.setdefault("context_metrics", []).append(metric)
            self.checkpoint(run)
            started = time.monotonic()
            call_readonly = getattr(self.transport, "call_readonly_auto", None)
            caller = (
                call_readonly
                if (
                    provider == "Auto"
                    and name == "propose_intent"
                    and state.get("source_mode") == "live"
                    and callable(call_readonly)
                )
                else self.transport.call
            )
            r = caller(
                provider,
                messages,
                tools=tools,
                tag=run["id"] + ":" + name + ":" + str(attempt),
                max_tokens=8192,
            )
            metric["elapsed_seconds"] = round(time.monotonic() - started, 4)
            recovered = r.get("read_only_failover")
            if recovered:
                # Account for BOTH independent provider inferences. The first
                # result is still UNKNOWN and may have consumed credit.
                run["receipts"].append(
                    {
                        "id": recovered["origin_id"],
                        "provider": "IISHKO",
                        "route": "IISHKO",
                        "state": "UNKNOWN_OUTCOME",
                        "inference_dispatched": True,
                        "automatic_redispatch": False,
                        "response_usable": False,
                        "routing": "READONLY_ORIGINAL_UNKNOWN",
                    }
                )
                run["read_only_failover"] = recovered
            run["receipts"].append(
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "provider",
                        "route",
                        "model",
                        "reported_model",
                        "http",
                        "state",
                        "usage",
                        "estimated_cost",
                        "unit",
                        "latency_sec",
                        "rotation_skips",
                        "automatic_redispatch",
                        "routing",
                        "read_only_failover",
                        "identity_status",
                        "identity_attested",
                        "response_usable",
                    )
                }
            )
            deadline.check()
            try:
                value, mode = decode(r, name, schema)
                checked = semantic(value) if semantic else value
                run["steps"].append(
                    {
                        "step": len(run["steps"]) + 1,
                        "tool": name,
                        "arguments": value,
                        "observation": {"ok": True, "mode": mode, "request_id": r["id"]},
                    }
                )
                return checked
            except (ContractError, Failure) as exc:
                if not completed(r):
                    raise
                error = str(exc)
                run["repairs"].append(
                    {"operation": name, "attempt": attempt, "error": error, "request_id": r["id"]}
                )
                if attempt:
                    raise Failure("SKILL_VALIDATION_FAILED:" + error) from exc

    def operation(self, run, names, state, operation, fn):
        self.progress(
            run,
            {
                "live_search": "SEARCHING",
                "archive_search": "SEARCHING",
                "select_evidence": "COMPARING",
                "prepare_preview": "PREPARING_PREVIEW",
                "inspect": "INSPECTING",
            }.get(operation, "VALIDATING"),
        )
        packs = self.registry.resolve(names)
        if operation not in set().union(*(set(p["operations"]) for p in packs)):
            raise Failure("SKILL_OPERATION_NOT_DECLARED")
        self.registry.context(names, state)
        for p in packs:
            run["active_skills"][p["id"]] = {"version": p["version"], "hash": p["hash"]}
        return fn()

    @deadline.request_budget()
    def run(
        self,
        text,
        provider="Auto",
        sid=None,
        revision=None,
        draft_id=None,
        request_id=None,
        source_mode="archive",
        use_jev=False,
    ):
        from city_names import geocoding_policy

        local = provider == "Local" or isinstance(self.transport, LocalSkillsTransport)
        with geocoding_policy(not local):
            return self._run(text, provider, sid, revision, draft_id, request_id, source_mode, use_jev)

    def _run(
        self, text, provider="Auto", sid=None, revision=None, draft_id=None,
        request_id=None, source_mode="archive", use_jev=False,
    ):
        if (
            not isinstance(text, str)
            or not 1 <= len(text.strip()) <= 1500
            or source_mode not in ("archive", "live")
            or type(use_jev) is not bool
        ):
            raise Failure("SKILL_INPUT")
        if any(ord(c) < 32 and c not in "\t\n\r" for c in text):
            raise Failure("SKILL_INPUT")
        text = re.sub(r"\bодин\s+номер\b", "1 номер", text, flags=re.I)
        latest_user_text = text
        if sid is None:
            from draft_input import prepare_local

            text = prepare_local(self.app, text, draft_id)
            from scoped_budget import initial_scope_reply

            pending_scope = initial_scope_reply(self.app, text, draft_id)
            if pending_scope is not None:
                pending_scope["agent"] = {
                    "mode": "OFFLINE_SKILLS", "status": "COMPLETE", "engine": "SKILLS_V1",
                    "external_calls": 0, "steps": [],
                }
                return pending_scope
        name, current, prefs = self.backend.snapshot(sid, revision)
        run = {
            "id": request_id or uuid.uuid4().hex,
            "status": "RUNNING",
            "engine": "SKILLS_V1",
            "provider": provider,
            "session_id": sid,
            "revision": revision,
            "source_mode": source_mode,
            "steps": [],
            "receipts": [],
            "repairs": [],
            "jev": [],
            "active_skills": {},
            "started_at": time.time(),
        }
        if not isinstance(run["id"], str) or not re.fullmatch("[A-Za-z0-9_-]{16,100}", run["id"]):
            raise Failure("SKILL_RUN_ID")
        with self.app.lock:
            previous = self.app.store.db.execute(
                "SELECT state FROM skill_runs WHERE id=?", (run["id"],)
            ).fetchone()
            if previous:
                raise Failure("SKILL_RUN_ALREADY_RECORDED_USE_HTTP_STATUS")
            self.save(run)
        run["time_budget"] = {
            "limit_seconds": deadline.DEFAULT_SECONDS,
            "remaining_seconds": round(deadline.remaining(), 3),
        }
        if provider == "Local" and source_mode == "archive":
            from offline_destinations import destination_reply

            out = destination_reply(text, current)
            if out is not None:
                run.update(
                    status="COMPLETE", stage="COMPLETE", finished_at=time.time(),
                    extraction="LOCAL_RULES", intent=out["language"]["validated_intent"],
                )
                run["steps"] = [{
                    "step": 1, "tool": "propose_destinations",
                    "observation": out["destination_proposals"],
                }]
                out["agent"] = {
                    "mode": "OFFLINE_SKILLS", "status": "COMPLETE", "engine": "SKILLS_V1",
                    "external_calls": 0, "trace_id": run["id"],
                    "steps": run["steps"],
                }
                checkpoint = getattr(self.app, "complete_skill_run", None)
                if callable(checkpoint):
                    checkpoint(run, out)
                else:
                    self.save(run)
                return out
        try:
            from scoped_budget import assess, render_scope_turn

            saved = self.app.store.get_session(sid) if sid else {}
            whole = (saved or {}).get("budget_scopes", {}).get("whole_trip")
            from pet_preferences import parse as pet_parse

            turn = assess(text, current, whole) if pet_parse(text) is None else None
        except Exception:
            run.update(status="FAILED", stage="FAILED", finished_at=time.time(), error="SCOPED_BUDGET")
            self.save(run)
            raise
        if turn is not None:
            out = render_scope_turn(self.app, sid, revision, turn)
            run.update(
                status="COMPLETE",
                stage="COMPLETE",
                finished_at=time.time(),
                extraction="LOCAL_RULES",
                intent={"intent": "clarify", "values": {}, "needs": []},
            )
            out["agent"] = {
                "mode": "OFFLINE_SKILLS",
                "status": "COMPLETE",
                "engine": "SKILLS_V1",
                "external_calls": 0,
                "trace_id": run["id"],
                "steps": run["steps"],
            }
            checkpoint = getattr(self.app, "complete_skill_run", None)
            if callable(checkpoint):
                checkpoint(run, out)
            else:
                self.save(run)
            return out
        observed = local_rules(text, current)
        schema = INTENT_SCHEMA
        if current is None and observed.get("intent") == "search":
            schema = copy.deepcopy(INTENT_SCHEMA)
            schema["properties"]["values"]["properties"].pop("text", None)
            schema["properties"]["values"]["properties"].pop("evidence_status", None)
            schema["properties"]["intent"]["enum"] = ["search"]
            schema["properties"]["values"]["required"] = [
                "city",
                "country",
                "checkin",
                "checkout",
                "rooms",
                "budget",
                "currency",
            ]
        state = {
            "latest_user_text": latest_user_text,
            "user_text_is_merged_draft": text != latest_user_text,
            "request_text": text,
            "current_request": jsonable(current) if current else None,
            "preferences": prefs,
            "revision": revision,
            "source_mode": source_mode,
            "allowed_fields": list(schema["properties"]["values"]["properties"]),
        }
        state["input_observations"] = {
            "source": "DETERMINISTIC_USER_TEXT_PARSER",
            "values": observed.get("values", {}),
            "needs": observed.get("needs", []),
            "note": "Observations only. Every proposed field still requires a verbatim user span and independent validation.",
        }
        try:
            if use_jev:
                try:
                    run["jev"].append(self.advise(state, run["id"] + ":jev:route"))
                except Exception as e:
                    run["jev"].append(
                        {"status": "UNAVAILABLE", "reason": type(e).__name__, "enforced": False}
                    )
            model_fallback = False
            try:
                intent, request, scenario_name, adjusted = self.ask(
                    provider,
                    state,
                    ["request-understanding"],
                    "propose_intent",
                    schema,
                    run,
                    lambda p: validate_proposal(p, text, current),
                )
            except Failure as exc:
                # No new inference is dispatched: an already-unknown provider
                # outcome stays unknown in the durable transport journal.
                # Use only independently complete user-text observations, and
                # require the owner to confirm the resulting preview.
                recoverable = {"NO_ELIGIBLE_MODEL_ROUTE", "PROVIDER_PREFLIGHT_UNAVAILABLE"}
                local = observed
                # A missing/contradictory request may only be clarified.
                # Never manufacture a preview or inferred booking from it.
                clarification = local.get("intent") == "clarify" and bool(local.get("needs"))
                complete = (
                    local.get("intent") in ("search", "budget", "refine", "constraints")
                    and not local.get("needs")
                    and bool(local.get("values"))
                )
                if (
                    provider != "Auto"
                    or source_mode != "live"
                    or str(exc) not in recoverable
                    or not (clarification or complete)
                ):
                    raise
                payload = {**local, "evidence": {k: text for k in local.get("values", {})}}
                # Validation enforces the same independent source spans,
                # budget arithmetic and request schema as model proposals.
                intent, request, scenario_name, adjusted = validate_proposal(payload, text, current)
                if clarification:
                    if request is not None or intent.get("intent") != "clarify":
                        raise
                elif request is None:
                    raise
                model_fallback = True
                run["model_fallback_reason"] = str(exc)
                run["unconfirmed_external_model_attempts"] = 1
            run.update(
                intent=intent,
                extraction="DETERMINISTIC_FALLBACK_MODEL_UNCONFIRMED"
                if model_fallback
                else ("LOCAL_RULES" if provider == "Local" else "MODEL_PROPOSED_BACKEND_VALIDATED"),
                validation_adjusted=adjusted,
            )
            if use_jev:
                try:
                    run["jev"].append(
                        self.advise({**state, "proposal": intent}, run["id"] + ":jev:verify")
                    )
                except Exception as e:
                    run["jev"].append(
                        {"status": "UNAVAILABLE", "reason": type(e).__name__, "enforced": False}
                    )
            changed = (
                invalidations(jsonable(current) if current else None, jsonable(request))
                if request
                else []
            )
            run["invalidated"] = changed
            kind = intent["intent"]
            final = ""
            grounding = []
            selection = {"offer_ids": [], "reason_ids": []}
            cards = {}
            facts = {}
            source = {"mode": source_mode, "status": "NOT_REQUESTED"}
            if request is not None and kind in ("search", "budget", "constraints", "refine"):
                self.backend.recheck_revision(sid, revision)
                search_state = {**state, "validated_request": jsonable(request)}
                if kind in ("budget", "constraints", "refine"):
                    self.operation(
                        run,
                        ["refine-and-repair"],
                        state,
                        "validate_constraints",
                        lambda: request.validate(),
                    )
                from hotel_requirements import combine

                requirements = combine([] if kind == "search" else prefs, text)

                def search():
                    if (
                        provider == "Local" and source_mode == "archive" and current is not None
                        and (current.checkin, current.checkout) != (request.checkin, request.checkout)
                    ):
                        return [], {
                            "mode": "archive", "status": "STALE", "source_refresh_required": True,
                        }
                    if requirements:
                        return self.backend.search(
                            request, scenario_name or name, source_mode, requirements=requirements
                        )
                    return self.backend.search(request, scenario_name or name, source_mode)

                offers, source = self.operation(
                    run,
                    ["hotel-search"],
                    search_state,
                    "live_search" if source_mode == "live" else "archive_search",
                    search,
                )
                cards, facts = self.backend.evidence(offers, source, request, run["id"], text=text)
                checked_state = {**search_state, "offers": cards, "facts": facts}
                self.operation(
                    run,
                    ["offer-evidence", "price-and-feasibility"],
                    checked_state,
                    "validate_constraints",
                    lambda: request.validate(),
                )
                run["steps"].append(
                    {
                        "step": len(run["steps"]) + 1,
                        "tool": "backend.search_and_compare",
                        "arguments": {"request": jsonable(request), "mode": source_mode},
                        "observation": {
                            "ok": True,
                            "status": source["status"],
                            "offers": len(offers),
                        },
                    }
                )
                eligible = [
                    k
                    for k, o in cards.items()
                    if o["status"] != "REJECT" and o.get("recommendable") is not False
                ]
                if eligible:
                    if source_mode == "live" and provider != "Local":
                        # A source-grounded shortlist needs no second model call.
                        # This removes the second SSE failure exposure while
                        # retaining real LLM interpretation of the user's request.
                        selection = self.backend.select_grounded(cards, facts)
                        run["selection_mode"] = "DETERMINISTIC_SOURCE_EVIDENCE"
                        run["steps"].append(
                            {
                                "step": len(run["steps"]) + 1,
                                "tool": "backend.select_grounded_evidence",
                                "arguments": {"maximum": 3},
                                "observation": {
                                    "ok": True,
                                    "selected": len(selection["offer_ids"]),
                                },
                            }
                        )
                    else:
                        presentation_state = {
                            **state,
                            "validated_request": jsonable(request),
                            "offers": {
                                k: {
                                    "name": o["_name"],
                                    "price": o["_price"],
                                    "currency": o["_currency"],
                                    "status": o["status"],
                                    "reasons": o["_reasons"],
                                    "supplier_facts": o.get("supplier_facts") or {},
                                }
                                for k, o in cards.items()
                            },
                            "facts": facts,
                        }
                        schema = presentation_schema(eligible, list(facts))

                        def check(value):
                            self.backend.present(value, cards, facts, source)
                            return value

                        selection = self.ask(
                            provider,
                            presentation_state,
                            ["compare-and-explain", "response-grounding"],
                            "select_evidence",
                            schema,
                            run,
                            check,
                        )
                        run["selection_mode"] = "MODEL_VALIDATED_SELECTION"
                explained = self.operation(
                    run,
                    ["compare-and-explain", "response-grounding"],
                    checked_state,
                    "compare",
                    lambda: self.backend.explain(selection, cards, facts, source),
                )
                final = explained["text"]
                grounding = explained["statements"]
                run["grounded_statements"] = grounding
                if not eligible:
                    self.operation(run, ["constraint-negotiation"], state, "clarify", lambda: None)
            elif kind == "clarify":
                self.operation(run, ["constraint-clarification"], state, "clarify", lambda: None)
            # Prepare state-changing proposals only after all model/semantic gates completed.
            out = self.operation(
                run,
                ["proposal-lifecycle"],
                state,
                "undo" if kind == "undo" else "prepare_preview",
                lambda: self.backend.prepare(
                    intent,
                    request,
                    scenario_name,
                    text,
                    sid,
                    revision,
                    draft_id,
                    source_result=source.get("result"),
                ),
            )
            if kind == "clarify":
                from natural_language import detect_date_error
                from draft_input import clarification_message

                message = clarification_message(self.app, draft_id, intent, text)
                from refinement import whole_trip_budget_in

                if current is not None and whole_trip_budget_in(text):
                    message = "Сохранён бюджет только на отель. Укажите бюджет на отель; общий бюджет поездки здесь не изменяется."
            elif kind == "inspect":
                message = (
                    "Условия отмены требуют подтверждения по конкретному тарифу; локальная отмена поездки не выполнялась."
                    if "cancellation" in intent.get("needs", [])
                    or re.search(r"услови[яй]\s+отмены", text, re.I)
                    else "Неподтверждённые условия сохранены. Налоги, вместимость и наличие требуют соответствующего источника."
                )
            elif kind == "undo":
                message = "Предыдущая локальная правка отменена."
            elif kind == "preference":
                message = "Пожелание подготовлено отдельно; свойства гостиниц не подтверждены."
            else:
                message = final + " Проверьте Preview: изменения ещё не применены."
                if source.get("status") == "STALE":
                    message = "STALE: даты изменятся; цены требуют нового поиска. Проверьте Preview: изменения ещё не применены."
            if kind == "inspect":
                from .answer_grounding import saved_inspection

                explained = saved_inspection(out, text, current)
                message = explained["text"]
                facts = explained["facts"]
                grounding = explained["statements"]
            elif not grounding:
                grounding = [{"text": message, "kind": "POLICY", "fact_ids": []}]
            else:
                grounding.append(
                    {
                        "text": "Проверьте Preview: изменения ещё не применены.",
                        "kind": "POLICY",
                        "fact_ids": [],
                    }
                )
            if model_fallback:
                warning = (
                    " Модель не завершила ответ. Недостающие условия определены локально; уточните их, чтобы продолжить."
                    if kind == "clarify"
                    else " Модель не завершила ответ. Условия разобраны локально; обязательно проверьте их перед подтверждением."
                )
                message += warning
                grounding.append({"text": warning.strip(), "kind": "POLICY", "fact_ids": []})
            if run.get("read_only_failover"):
                warning = " IISHKO не подтвердил завершение ответа; запрос обработала резервная модель InkStone."
                message += warning
                grounding.append({"text": warning.strip(), "kind": "POLICY", "fact_ids": []})
            run["grounded_statements"] = grounding
            run["time_budget"]["remaining_seconds"] = round(deadline.remaining(), 3)
            run.update(
                status="COMPLETE",
                stage="COMPLETE",
                finished_at=time.time(),
                selection=selection,
                comparison={
                    "offers": list(cards),
                    "source": {k: v for k, v in source.items() if k != "result"},
                },
                evidence=facts,
            )
            count = (
                0
                if provider == "Local"
                else max(len(run["receipts"]), run.get("unconfirmed_external_model_attempts", 0))
            )
            out.update(
                language_message=message,
                language={
                    "validated_intent": intent,
                    "source": "LOCAL_VALIDATED_FALLBACK"
                    if model_fallback
                    else ("LOCAL_SKILLS" if provider == "Local" else "SKILLS_MODEL"),
                    "model_calls": count,
                },
                agent={
                    "mode": "LIVE_WITH_LOCAL_INTERPRETATION_FALLBACK"
                    if model_fallback
                    else ("OFFLINE_SKILLS" if provider == "Local" else "REAL_HTTPS_SKILLS"),
                    "status": "COMPLETE",
                    "engine": "SKILLS_V1",
                    "external_calls": count,
                    "trace_id": run["id"],
                    "steps": run["steps"],
                    "comparison": run["comparison"],
                    "extraction": run["extraction"],
                    "selection_mode": run.get("selection_mode", "NONE"),
                    "model_fallback_reason": run.get("model_fallback_reason"),
                    "read_only_failover": run.get("read_only_failover"),
                    "repairs": run["repairs"],
                    "jev": run["jev"],
                    "invalidated": changed,
                    "skill_versions": run["active_skills"],
                    "context_metrics": run.get("context_metrics", []),
                    "facts": facts,
                    "grounded_statements": grounding,
                    "source_result": source.get("result"),
                },
            )
            from pet_preferences import annotate_turn

            annotate_turn(out, text, intent)
            checkpoint = getattr(self.app, "complete_skill_run", None)
            if callable(checkpoint):
                checkpoint(run, out)
            else:
                self.save(run)
            return out
        except Exception as exc:
            run["time_budget"]["remaining_seconds"] = round(deadline.remaining(), 3)
            run.update(
                status="FAILED",
                stage="FAILED",
                error=str(exc) if isinstance(exc, Failure) else type(exc).__name__,
                finished_at=time.time(),
            )
            self.save(run)
            raise


class LocalSkillsTransport:
    """Explicit offline demonstration of the same contracts, never model evidence."""

    def call(self, p, messages, tools=None, tag=None, max_tokens=None):
        from natural_language import local_rules
        from app import request_from

        envelope = json.loads(messages[-1]["content"])
        state = envelope["context"]["state"]
        name = tools[0]["function"]["name"]
        if name == "propose_intent":
            current = request_from(state["current_request"]) if state["current_request"] else None
            raw = local_rules(state["request_text"], current)
            value = {**raw, "evidence": {k: state["request_text"] for k in raw["values"]}}
        else:
            ids = tools[0]["function"]["parameters"]["properties"]["offer_ids"]["items"]["enum"][:3]
            facts = envelope["source_data"]["fields"]["facts"]
            value = {
                "offer_ids": ids,
                "reason_ids": [k for k, v in facts.items() if v["offer_id"] in ids][:12],
            }
        return {
            "id": tag,
            "provider": "Local",
            "model": "LOCAL_RULES",
            "state": "RESPONSE_RECEIVED",
            "http": 200,
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": uuid.uuid4().hex,
                                "type": "function",
                                "function": {"name": name, "arguments": canonical(value)},
                            }
                        ],
                    },
                }
            ],
        }
