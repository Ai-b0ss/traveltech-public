"""Skills routing: account-scoped quarantine, durable run pinning, no post retry."""

import deadline_http
import request_deadline as deadline
import hashlib, json, sqlite3, threading, time, urllib.request, urllib.error, uuid, os
from contextlib import contextmanager, closing
from pathlib import Path
from provider_transport import Failure, strict_json, canonical, now, NoRedirect
from .model_policy import (
    run_key,
    scope,
    blocked_reason,
    operation_hash,
    retry_seconds,
    response_record,
    compact_record,
    authorized_readonly_recovery,
)
from .task_routing import preferred_routes, PRIORITY

LOGICC_MODELS = {
    "Logicc_GPT6": "gpt-6.1-sol",
    "Logicc_Opus": "claude-5.5-opus",
    "Logicc_Astra": "gpt-6-astra",
    "Logicc_Sonnet": "claude-5.5-sonnet",
}
MODELS = LOGICC_MODELS


class TravelModelLadder:
    def __init__(
        self, delegate, state, base_url="http://127.0.0.1:18789", opener=None, history_paths=None
    ):
        self.delegate = delegate
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self.opener = opener or deadline_http.opener(NoRedirect())
        self.jev = delegate.jev
        self.lock = threading.RLock()
        self.sticky = {}
        self.history_paths = [
            Path(p)
            for p in (
                history_paths
                if history_paths is not None
                else os.environ.get("TRAVELTECH_MODEL_HISTORY", "").split(os.pathsep)
            )
            if p
        ]
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempts(tag TEXT PRIMARY KEY,record TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS read_only_failovers(origin_id TEXT PRIMARY KEY,record TEXT NOT NULL)"
            )
        self.purge_payloads()

    @contextmanager
    def db(self):
        with closing(
            sqlite3.connect(self.state / "travel_model_ladder.sqlite", timeout=30)
        ) as conn:
            with conn:
                yield conn

    def rows(self):
        with self.db() as db:
            mine = [
                strict_json(r[0]) for r in db.execute("SELECT record FROM attempts ORDER BY rowid")
            ]
        # Read the established legacy journal too; do not hide existing usage by
        # constructing a new adapter. It is metadata, not another inference path.
        old = self.state.parent / "logicc/logicc.sqlite"
        if old.is_file():
            with closing(sqlite3.connect(old.as_uri() + "?mode=ro", uri=True)) as db:
                mine += [
                    strict_json(r[0])
                    for r in db.execute("SELECT record FROM requests ORDER BY rowid")
                ]
        for path in self.history_paths:
            if not path.is_file():
                raise Failure("HISTORICAL_MODEL_JOURNAL_MISSING")
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                mine += [
                    strict_json(r[0])
                    for r in db.execute("SELECT record FROM attempts ORDER BY rowid")
                ]
        result = []
        seen = set()
        for row in mine + self.delegate.route_rows():
            identity = canonical(row)
            if identity not in seen:
                seen.add(identity)
                result.append(row)
        return result




    def _admission_reason(self, rows, route, op_hash=None, tag=None, db=None):
        if route == "IISHKO" and hasattr(self.delegate, "route_blocked_reason"):
            return self.delegate.route_blocked_reason(rows, route, op_hash)
        return blocked_reason(rows, route, op_hash)


    def stopped(self):
        obj = self
        seen = set()
        while obj is not None and id(obj) not in seen:
            seen.add(id(obj))
            state = getattr(obj, "state", None)
            if state and any(
                (Path(state) / p).exists() for p in ("STOP_INFERENCE", "../STOP_INFERENCE")
            ):
                return True
            obj = getattr(obj, "delegate", None)
        return False

    def acknowledge_run(self, key):
        with self.db() as db:
            for tag, raw in db.execute("SELECT tag,record FROM attempts").fetchall():
                r = strict_json(raw)
                if run_key(tag) == key and r.get("state") == "RESPONSE_RECEIVED":
                    r.setdefault("application_processed_at", time.time())
                    db.execute("UPDATE attempts SET record=? WHERE tag=?", (canonical(r), tag))
        fn = getattr(self.delegate, "acknowledge_run", None)
        if callable(fn):
            fn(key)

    def purge_payloads(self, at=None):
        with self.db() as db:
            for tag, raw in db.execute("SELECT tag,record FROM attempts").fetchall():
                r = strict_json(raw)
                new = compact_record(r, at)
                if new != r:
                    db.execute("UPDATE attempts SET record=? WHERE tag=?", (canonical(new), tag))
        fn = getattr(self.delegate, "purge_payloads", None)
        if callable(fn):
            fn(at)


    def route_status(self):
        return {
            r: {
                "account_scope": scope(r),
                "blocked_reason": self.model_available(r),
                "enabled": self.model_available(r) is None,
                "catalog": "CHECKED_BEFORE_DISPATCH",
            }
            for r in preferred_routes([])[0]
        }


    def _reserve(self, tag, record):
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            history = [strict_json(r[0]) for r in db.execute("SELECT record FROM attempts")]
            if any(r.get("tag") == tag for r in history):
                raise Failure("REQUEST_ID_ALREADY_USED")
            # Re-read configured histories inside the reservation transaction.
            all_rows = self.rows()
            reason = self._admission_reason(
                all_rows, record["route"], record.get("operation_hash"), tag, db
            )
            if reason:
                raise Failure(reason)
            same = [r for r in history if run_key(r.get("tag")) == run_key(tag)]
            if any(
                r.get("state") != "RESPONSE_RECEIVED"
                or r.get("response_usable") is False
                or r.get("route") != record["route"]
                for r in same
            ):
                raise Failure("RUN_ROUTE_OR_OUTCOME_CONFLICT")
            db.execute("INSERT INTO attempts VALUES(?,?)", (tag, canonical(record)))

    def model_available(self, route, op_hash=None, tag=None):
        # Retired external gateways are not eligible for new requests.
        if route in LOGICC_MODELS or route.startswith("Omnirush"):
            return "MODEL_ROUTE_DISABLED"
        deadline.check()
        rows = self.rows()
        reason = self._admission_reason(rows, route, op_hash, tag)
        return reason or self.delegate.reason_to_skip(route)


    def _dispatch_logicc(self, route, messages, tools=None, tag=None, max_tokens=None):
        model = LOGICC_MODELS[route]
        tag = tag or uuid.uuid4().hex
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": 0,
            "max_tokens": max_tokens or 4096,
        }
        if tools:
            payload.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        raw = canonical(payload).encode("utf-8")
        if (
            len(raw) > 240000
            or type(payload["max_tokens"]) is not int
            or not 1 <= payload["max_tokens"] <= 32768
        ):
            raise Failure("REQUEST_BODY_LIMIT")
        record = {
            "id": uuid.uuid4().hex,
            "tag": tag,
            "route": route,
            "provider": route,
            "model": model,
            "account_scope": scope(route),
            "state": "RESERVED",
            "at": now(),
            "http": None,
            "automatic_redispatch": False,
            "operation_hash": operation_hash(messages, tools),
            "payload_hash": hashlib.sha256(raw).hexdigest(),
        }
        deadline.check()
        self._reserve(tag, record)
        start = time.monotonic()
        dispatched = False
        request = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=raw,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Request-ID": record["id"],
            },
            method="POST",
        )
        try:
            socket_timeout = deadline.timeout(180)
            dispatched = True
            with self.opener.open(request, timeout=socket_timeout) as response:
                record["http"] = response.status
                body = deadline.read(response, 4000001)
            record["state"] = "RESPONSE_RECEIVED"
            record["response_sha256"] = hashlib.sha256(body).hexdigest()
            if len(body) > 4000000:
                record.update(response_usable=False, error="RESPONSE_TOO_LARGE")
            else:
                try:
                    record.update(response_record(strict_json(body), model))
                except (ValueError, UnicodeError):
                    record.update(response_usable=False, error="INFERENCE_ENVELOPE_INVALID")
        except urllib.error.HTTPError as exc:
            record.update(state="HTTP_ERROR", http=exc.code, error="UPSTREAM_HTTP_" + str(exc.code))
            if exc.code == 429 or exc.code in (500, 502, 503, 504):
                record["retry_at"] = time.time() + retry_seconds(
                    exc.headers.get("Retry-After") if exc.headers else None,
                    default=60 if exc.code == 429 else 30,
                )
        except Exception as exc:
            record.update(
                state="UNKNOWN_OUTCOME" if dispatched else "LOCAL_FAILURE",
                inference_dispatched=dispatched,
                error=str(exc) if isinstance(exc, Failure) else type(exc).__name__,
            )
        record["latency_sec"] = round(time.monotonic() - start, 3)
        with self.db() as db:
            db.execute("UPDATE attempts SET record=? WHERE tag=?", (canonical(record), tag))
        return record


    @contextmanager
    def provider_scope(self, route):
        yield


    def call_readonly_auto(self, provider, messages, tools=None, tag=None, max_tokens=None):
        # Only the Skills runtime's model interpretation operation invokes this.
        return self.call(
            provider,
            messages,
            tools=tools,
            tag=tag,
            max_tokens=max_tokens,
            safe_readonly_failover=True,
        )

    def _recover_iishko_readonly(
        self, original, messages, tools, tag, max_tokens, skipped, complexity
    ):
        # An UNKNOWN result remains UNKNOWN in its original account journal.
        # Authorize only one independent inference on the InkStone account.
        if (
            original.get("route") != "IISHKO"
            or original.get("state") != "UNKNOWN_OUTCOME"
            or original.get("response_usable") is True
            or not original.get("id")
        ):
            raise Failure("READONLY_FAILOVER_ORIGIN_INVALID")
        old = next(
            (
                x
                for x in self.delegate.route_rows()
                if x.get("id") == original["id"]
                and x.get("tag") == original.get("tag")
                and x.get("provider") == "IISHKO"
                and x.get("state") == "UNKNOWN_OUTCOME"
            ),
            None,
        )
        if old is None:
            raise Failure("READONLY_FAILOVER_ORIGIN_NOT_DURABLE")
        if old.get("operation_hash") != operation_hash(messages, tools):
            raise Failure("READONLY_FAILOVER_FINGERPRINT_CONFLICT")
        target = "InkStone_AgentsA1"
        # Respect target account quarantine, health and STOP flags as usual.
        reason = self.model_available(target, old["operation_hash"], tag)
        if reason and reason != "UNKNOWN_OPERATION_NO_REPLAY":
            raise Failure("READONLY_FAILOVER_TARGET_UNAVAILABLE:" + reason)
        child = "rf" + hashlib.sha256((original["id"] + ":" + target).encode()).hexdigest()[:28]
        child_tag = child + ":readonly_recovery:" + target
        proof = {
            "origin_id": original["id"],
            "origin_tag": original["tag"],
            "operation_hash": old["operation_hash"],
            "target_route": target,
        }
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM read_only_failovers WHERE origin_id=?", (original["id"],)
            ).fetchone():
                raise Failure("READONLY_FAILOVER_ALREADY_RESERVED")
            entry = {
                **proof,
                "new_tag": child_tag,
                "state": "AUTHORIZED",
                "at": now(),
                "original_state": "UNKNOWN_OUTCOME",
                "automatic_redispatch": False,
            }
            db.execute(
                "INSERT INTO read_only_failovers VALUES(?,?)", (original["id"], canonical(entry))
            )
        response = None
        try:
            with authorized_readonly_recovery(proof):
                response = self.delegate.call(
                    target, messages, tools=tools, tag=child_tag, max_tokens=max_tokens
                )
        except Failure:
            # Reservation is intentionally never undone. Another attempt
            # would risk duplicate provider inference after an unknown reply.
            raise
        finally:
            with self.lock, self.db() as db:
                row = db.execute(
                    "SELECT record FROM read_only_failovers WHERE origin_id=?", (original["id"],)
                ).fetchone()
                if row:
                    updated = strict_json(row[0])
                    updated["state"] = "RETURNED" if response is not None else "FAILED_OR_UNKNOWN"
                    updated["finished_at"] = time.time()
                    db.execute(
                        "UPDATE read_only_failovers SET record=? WHERE origin_id=?",
                        (canonical(updated), original["id"]),
                    )
        return {
            **response,
            "route": target,
            "rotation_skips": skipped,
            "routing": "READONLY_CROSS_ACCOUNT_FAILOVER",
            "read_only_failover": {
                "origin_id": original["id"],
                "origin_route": "IISHKO",
                "origin_state": "UNKNOWN_OUTCOME",
                "target_route": target,
            },
            "automatic_redispatch": False,
            "complexity_class": complexity,
            "requested_model": "Agents-A1",
        }

    def call(
        self,
        provider,
        messages,
        tools=None,
        tag=None,
        max_tokens=None,
        safe_readonly_failover=False,
    ):
        if provider in LOGICC_MODELS or provider.startswith("Omnirush"):
            raise Failure("MODEL_ROUTE_DISABLED")
        deadline.check()
        if self.stopped():
            raise Failure("STOP_REQUESTED")
        self.purge_payloads()
        tag = tag or uuid.uuid4().hex
        key = run_key(tag)
        records = self.rows()
        fingerprint = operation_hash(messages, tools)
        # An unknown operation cannot be revived under a different run/model ID.
        if any(
            r.get("state") in ("RESERVED", "UNKNOWN_OUTCOME")
            and r.get("operation_hash") == fingerprint
            for r in records
        ):
            raise Failure("UNKNOWN_OPERATION_NO_REPLAY")
        history = [r for r in records if run_key(r.get("tag")) == key]
        routes = {r.get("route") or r.get("provider") for r in history}
        if len(routes) > 1 or any(
            r.get("state") != "RESPONSE_RECEIVED" or r.get("response_usable") is False
            for r in history
        ):
            raise Failure("RUN_ROUTE_OR_OUTCOME_CONFLICT")
        pinned = next(iter(routes)) if routes else self.sticky.get(key)
        if pinned and provider not in ("Auto", pinned):
            raise Failure("MODEL_SWITCH_DURING_RUN_FORBIDDEN")
        if provider in LOGICC_MODELS or provider.startswith("Omnirush"):
            raise Failure("MODEL_ROUTE_DISABLED")
        if provider == "Auto":
            ranked, complexity = preferred_routes(messages)
            routes = (pinned,) if pinned else ranked
        elif provider in MODELS or provider in ("IISHKO", "InkStone_Qwen"):
            routes = (provider,)
            complexity = "MANUAL"
        else:
            return self.delegate.call(
                provider, messages, tools=tools, tag=tag, max_tokens=max_tokens
            )
        skipped = []
        for route in routes:
            with self.provider_scope(route):
                reason = self.model_available(route, fingerprint, tag)
                if reason:
                    if pinned:
                        raise Failure("PINNED_PROVIDER_UNAVAILABLE_NO_RETRY")
                    skipped.append(
                        {"route": route, "reason": reason, "inference_dispatched": False}
                    )
                    continue
                actual_tag = tag + ":" + route
                if any(r.get("tag") == actual_tag for r in records):
                    raise Failure("REQUEST_ID_ALREADY_USED")
                if route in LOGICC_MODELS:
                    receipt = self._dispatch_logicc(route, messages, tools, actual_tag, max_tokens)
                else:
                    try:
                        receipt = self.delegate.call(
                            route, messages, tools=tools, tag=actual_tag, max_tokens=max_tokens
                        )
                    except Failure as exc:
                        emitted = any(
                            isinstance(r.get("tag"), str)
                            and (r["tag"] == actual_tag or r["tag"].startswith(actual_tag + ":"))
                            for r in self.rows()
                        )
                        if self.stopped():
                            raise Failure("STOP_REQUESTED")
                        if pinned or emitted or not str(exc).startswith("NO_SAFE_MODEL_ROUTE:"):
                            raise
                        skipped.append(
                            {
                                "route": route,
                                "reason": "PROVIDER_PREFLIGHT_REFUSED",
                                "inference_dispatched": False,
                            }
                        )
                        continue
                self.sticky[key] = route
                if (
                    safe_readonly_failover
                    and provider == "Auto"
                    and not pinned
                    and route == "IISHKO"
                    and receipt.get("state") == "UNKNOWN_OUTCOME"
                ):
                    return self._recover_iishko_readonly(
                        receipt, messages, tools, tag, max_tokens, skipped, complexity
                    )
                if receipt.get("response_usable") is False:
                    raise Failure(receipt.get("error", "INFERENCE_ENVELOPE_INVALID"))
                return {
                    **receipt,
                    "route": route,
                    "rotation_skips": skipped,
                    "automatic_redispatch": False,
                    "routing": "PRIORITY_PRE_DISPATCH",
                    "complexity_class": complexity,
                    "requested_model": MODELS.get(route, route),
                    "identity_attested": receipt.get("identity_attested", False),
                }
        raise Failure("NO_ELIGIBLE_MODEL_ROUTE")
