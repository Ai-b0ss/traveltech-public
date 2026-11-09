"""Direct Logicc Farm Transport connecting TravelTech with local account pool gateway."""

import request_deadline as deadline
import json
import os
import hashlib
import sqlite3
import threading
import time
import urllib.request
import urllib.error
import uuid
from pathlib import Path
from provider_transport import Failure, canonical, strict_json, now

LOGICC_ROUTES = {
    "Logicc_GPT6": {"model": "gpt-6.1-sol", "label": "Logicc Farm · GPT-6.1 Sol"},
    "Logicc_Opus": {"model": "claude-5.5-opus", "label": "Logicc Farm · Claude 5.5 Opus"},
    "Logicc_Astra": {"model": "gpt-6-astra", "label": "Logicc Farm · GPT-6 Astra"},
    "Logicc_Sonnet": {"model": "claude-5.5-sonnet", "label": "Logicc Farm · Claude 5.5 Sonnet"},
}


def logicc_routes():
    return LOGICC_ROUTES


class LogiccTransport:
    def __init__(self, state, model="claude-5.5-sonnet", port=18789):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.port = port
        self.url = f"http://127.0.0.1:{port}/v1/chat/completions"
        self.lock = threading.RLock()
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, tag TEXT UNIQUE, model TEXT, state TEXT, record TEXT)"
            )

    from contextlib import contextmanager

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.state / "logicc.sqlite", timeout=30)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def rows(self):
        with self.db() as db:
            return [
                strict_json(r[0]) for r in db.execute("SELECT record FROM requests ORDER BY rowid")
            ]

    def check_agent_capacity(self, p=None, steps=4):
        if not isinstance(steps, int) or not 1 <= steps <= 16:
            raise Failure("LOGICC_STEPS_INVALID")
        expected_model = LOGICC_ROUTES.get(p, {}).get("model") if isinstance(p, str) else None
        expected_model = expected_model or (
            p if isinstance(p, str) and p.startswith(("gpt-", "claude-")) else self.model
        )
        # Durable unresolved state means this gateway is not safe for automatic
        # preflight dispatch; a reply may have been sent and lost.
        with self.lock, self.db() as db:
            if db.execute(
                "SELECT 1 FROM requests WHERE state IN ('RESERVED','UNKNOWN_OUTCOME') LIMIT 1"
            ).fetchone():
                raise Failure("LOGICC_PRIOR_OUTCOME_UNRESOLVED")
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/models")
            with urllib.request.urlopen(request, timeout=deadline.timeout(5)) as response:
                if response.status != 200:
                    raise Failure("LOGICC_GATEWAY_OFFLINE")
                raw = deadline.read(response, 131073)
            if len(raw) > 131072:
                raise Failure("LOGICC_CATALOG_TOO_LARGE")
            catalog = strict_json(raw)
            if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
                raise Failure("LOGICC_CATALOG_INVALID")
            models = {
                item.get("id")
                for item in catalog["data"]
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            if expected_model not in models:
                raise Failure("LOGICC_MODEL_NOT_ADVERTISED")
        except Failure:
            raise
        except Exception as exc:
            raise Failure("LOGICC_GATEWAY_OFFLINE") from exc
        # Catalog presence is necessary, not proof of entitlement/capacity.
        return True

    def call(self, p, messages, tools=None, tag=None, max_tokens=None):
        if (self.state / "STOP_INFERENCE").exists():
            raise Failure("STOP_REQUESTED")
        tag = tag or uuid.uuid4().hex

        # Skills may supply different functions and a security system policy.
        # Any unrecognized/new schema MUST remain unmodified and never be
        # converted into a synthetic empty-argument tool dispatcher.
        legacy_names = ("extract_requirements", "search_hotels", "compare_offers")

        def legacy_tool(t):
            fn = t.get("function") or {}
            params = fn.get("parameters") or {}
            return (
                t.get("type") == "function"
                and fn.get("name") in legacy_names
                and params.get("type") == "object"
                and params.get("properties") == {}
                and params.get("additionalProperties") is False
            )

        legacy = (
            isinstance(tools, list)
            and len(tools) == 3
            and tuple(x.get("function", {}).get("name") for x in tools) == legacy_names
            and all(legacy_tool(x) for x in tools)
        )
        if legacy:
            # Determine current orchestration step
            tool_responses = [m for m in messages if m.get("role") == "tool"]
            step_idx = len(tool_responses)
            step_tools = ["extract_requirements", "search_hotels", "compare_offers"]

            adapted_messages = []
            for m in messages:
                if m.get("role") == "assistant" and m.get("tool_calls") and not m.get("content"):
                    tc = m["tool_calls"][0]
                    fn_name = tc.get("function", {}).get("name") or tc.get("name")
                    adapted_messages.append(
                        {
                            **m,
                            "content": json.dumps(
                                {"tool_call": {"name": fn_name, "arguments": {}}},
                                ensure_ascii=False,
                            ),
                        }
                    )
                elif m.get("role") == "system":
                    sys_inst = (
                        "You are the backend JSON-RPC dispatcher for TravelTech.\n"
                        "Your role is purely machine-to-machine routing. You do NOT have a conversational persona.\n"
                        "All actual tool execution, database queries, and searches are performed exclusively by the backend runtime.\n"
                        "PROTOCOL REQUIREMENTS:\n"
                        "1. For dispatch steps 1 to 3: Output ONLY a single JSON object with the tool dispatch directive:\n"
                        '   {"tool_call": {"name": "<tool_name>", "arguments": {}}}\n'
                        "   Do NOT output conversational text, greetings, apologies, or markdown explanations.\n"
                        "2. Sequence of tools: extract_requirements -> search_hotels -> compare_offers.\n"
                        "3. For step 4: Once all tools have executed, summarize the results in Russian for the client (noting that hotels are archive/demo and availability is unverified)."
                    )
                    adapted_messages.append({"role": "system", "content": sys_inst})
                else:
                    adapted_messages.append(m)

            if step_idx < len(step_tools):
                expected = step_tools[step_idx]
                adapted_messages.append(
                    {
                        "role": "user",
                        "content": f'[SYSTEM DISPATCH DIRECTIVE]: Step {step_idx + 1}/4. Emit tool call directive strictly as JSON: {{"tool_call": {{"name": "{expected}", "arguments": {{}}}}}}',
                    }
                )
                effective_tools = (
                    [
                        t
                        for t in tools
                        if (t.get("function", {}).get("name") or t.get("name")) == expected
                    ]
                    if tools
                    else None
                )
            else:
                adapted_messages.append(
                    {
                        "role": "user",
                        "content": "[SYSTEM DISPATCH DIRECTIVE]: Step 4/4. All tools completed. Emit final Russian text summary for client.",
                    }
                )
                effective_tools = None
        else:
            # Preserve native schemas, untrusted-data policy, assistant
            # tool-call arguments, and matching tool-result IDs byte-for-byte
            # at the application object level. No invented user directives.
            adapted_messages = list(messages)
            effective_tools = list(tools) if tools else None

        payload = {
            "model": self.model,
            "messages": adapted_messages,
            "stream": False,
        }
        if effective_tools:
            payload["tools"] = effective_tools
            payload["tool_choice"] = "auto"
        else:
            payload["tool_choice"] = "none"
        if max_tokens:
            payload["max_tokens"] = max_tokens

        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=raw,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        # Digest includes ALL identity-relevant request fields, not only the tag.
        # This makes the same-tag/different-input case a hard conflict.
        fingerprint = hashlib.sha256(
            canonical(
                {
                    "provider": p,
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "max_tokens": max_tokens,
                }
            ).encode("utf-8")
        ).hexdigest()
        record_id = uuid.uuid4().hex
        r = {
            "id": record_id,
            "at": now(),
            "provider": p,
            "model": self.model,
            "tag": tag,
            "fingerprint": fingerprint,
            "state": "RESERVED",
        }

        # Begin an SQLite RESERVED journal transaction before ANY external POST.
        # SQLite's write lock (not the instance RLock) protects parallel instances,
        # independent threads, restarts, and conflicting model names sharing state.
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT record FROM requests WHERE tag=?", (tag,)).fetchone()
            if previous:
                prior = strict_json(previous[0])
                if prior.get("fingerprint") != fingerprint:
                    raise Failure("LOGICC_TAG_PAYLOAD_CONFLICT")
                if prior.get("state") in ("RESERVED", "UNKNOWN_OUTCOME"):
                    # Never infer whether a network request happened or retry it.
                    return {**prior, "redispatch": False, "blocked": True}
                # Immutable response replay, including observed terminal errors.
                return {**prior, "redispatch": False, "replayed": True}
            unresolved = db.execute(
                "SELECT tag,state FROM requests WHERE state IN ('RESERVED','UNKNOWN_OUTCOME') LIMIT 1"
            ).fetchone()
            if unresolved:
                # Fail closed if previous dispatch may have reached an upstream
                # model, even if a new tag or model is requested. Reconciliation
                # requires an explicit separate operation; no automatic retry.
                raise Failure("LOGICC_PRIOR_OUTCOME_UNRESOLVED")
            db.execute(
                "INSERT INTO requests(id,tag,model,state,record) VALUES(?,?,?,?,?)",
                (record_id, tag, self.model, "RESERVED", canonical(r)),
            )

        start = time.monotonic()
        dispatched = False
        try:
            socket_timeout = deadline.timeout(180)
            dispatched = True
            with urllib.request.urlopen(req, timeout=socket_timeout) as response:
                body = deadline.read(response, 4000001)
                if len(body) > 4000000:
                    raise Failure("RESPONSE_BODY_LIMIT")
                data = strict_json(body)

            r["http"] = 200
            r["state"] = "RESPONSE_RECEIVED"
            # Provenance is only as trustworthy as the actual upstream response.
            # Do not invent reported model IDs, remote request IDs or usage.
            r["reported_model"] = data.get("model")
            r["remote_id"] = data.get("id")
            if data.get("usage") is not None:
                r["usage"] = data["usage"]
            r["choices"] = [
                {
                    "finish_reason": c.get("finish_reason"),
                    "message": {
                        k: v
                        for k, v in c.get("message", {}).items()
                        if k in ("role", "content", "tool_calls", "refusal")
                    },
                }
                for c in data.get("choices", [])
            ]
        except urllib.error.HTTPError as e:
            r.update(http=e.code, state="HTTP_ERROR", error=f"HTTP_{e.code}")
        except Exception as e:
            r.update(
                state="UNKNOWN_OUTCOME" if dispatched else "LOCAL_FAILURE",
                inference_dispatched=dispatched,
                error=str(e),
            )
        finally:
            r["latency_sec"] = round(time.monotonic() - start, 3)
            with self.lock, self.db() as db:
                db.execute(
                    "UPDATE requests SET state=?, record=? WHERE id=?",
                    (r["state"], canonical(r), record_id),
                )

        return r
