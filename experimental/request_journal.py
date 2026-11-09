"""HTTP delivery, processed-model receipts and bounded dialogue history.

Bodies expire; request IDs, fingerprints, outcomes and cost journals never reset.
No recovery method dispatches an inference or supplier request.
"""

import hashlib, json, time, threading, sqlite3
from provider_transport import Failure, canonical


class RequestJournalMixin:
    def init_request_journal(self):
        self.store.db.executescript("""
  CREATE TABLE IF NOT EXISTS http_request_meta(id TEXT PRIMARY KEY,path TEXT NOT NULL,started_at REAL NOT NULL,completed_at REAL,ui_ack_at REAL,body_sha256 TEXT,original_code INTEGER);
  CREATE TABLE IF NOT EXISTS dialogue_history(id TEXT PRIMARY KEY,owner TEXT NOT NULL,draft_id TEXT,sid TEXT,user_text TEXT,assistant_text TEXT,created_at REAL NOT NULL);
  CREATE INDEX IF NOT EXISTS dialogue_owner_draft ON dialogue_history(owner,draft_id);
  CREATE INDEX IF NOT EXISTS dialogue_owner_sid ON dialogue_history(owner,sid);
  """)
        with self.lock, self.store.transaction():
            # Old rows get a new retention grace period, not a fabricated completion.
            self.store.db.execute(
                "INSERT OR IGNORE INTO http_request_meta(id,path,started_at,completed_at) SELECT id,'legacy',?,CASE WHEN state='DONE' THEN ? ELSE NULL END FROM http_requests",
                (time.time(), time.time()),
            )
        self._journal_cleanup_at = 0
        self.cleanup_request_journal()
        self._journal_stop = threading.Event()

        def maintain():
            while not self._journal_stop.wait(60):
                try:
                    self.cleanup_request_journal()
                    for target in (self.skill_transport, self.public):
                        fn = getattr(target, "purge_payloads", None) or getattr(
                            target, "cleanup_payloads", None
                        )
                        if callable(fn):
                            fn()
                except (OSError, ValueError, sqlite3.Error):
                    pass

        self._journal_thread = threading.Thread(
            target=maintain, name="traveltech-journal-retention", daemon=True
        )
        self._journal_thread.start()

    def close(self):
        stop = getattr(self, "_journal_stop", None)
        if stop is not None:
            stop.set()
            self._journal_thread.join(timeout=5)
            if self._journal_thread.is_alive():
                raise Failure("JOURNAL_HOUSEKEEPING_STOP_TIMEOUT")
        return super().close()

    def start_http_metadata(self, rid, owner, path, body):
        self.store.db.execute(
            "INSERT INTO http_request_meta(id,path,started_at) VALUES(?,?,?)",
            (rid, path, time.time()),
        )
        if path in ("/api/skills/agent", "/api/language") and isinstance(body.get("text"), str):
            self.store.db.execute(
                "INSERT INTO dialogue_history VALUES(?,?,?,?,?,?,?)",
                (
                    rid,
                    owner,
                    body.get("draft_id"),
                    body.get("sid"),
                    body["text"][:1500],
                    None,
                    time.time(),
                ),
            )

    def finish_http_metadata(self, rid, code, data):
        encoded = canonical(data)
        self.store.db.execute(
            "UPDATE http_request_meta SET completed_at=COALESCE(completed_at,?),body_sha256=?,original_code=? WHERE id=?",
            (time.time(), hashlib.sha256(encoded.encode()).hexdigest(), code, rid),
        )
        if isinstance(data, dict):
            answer = data.get("language_message") or data.get("error")
            if isinstance(answer, str):
                self.store.db.execute(
                    "UPDATE dialogue_history SET assistant_text=? WHERE id=?", (answer[:20000], rid)
                )

    def bind_dialogue(self, owner, preview_id, sid):
        row = self.store.db.execute(
            "SELECT id FROM draft_groups WHERE preview_id=?", (preview_id,)
        ).fetchone()
        if row:
            self.store.db.execute(
                "UPDATE dialogue_history SET sid=? WHERE owner=? AND draft_id=? AND sid IS NULL",
                (sid, owner, row[0]),
            )

    def complete_http_result(self, owner, rid, code, data):
        with self.lock, self.store.transaction():
            row = self.store.db.execute(
                "SELECT state,code,response FROM http_requests WHERE id=? AND owner=?", (rid, owner)
            ).fetchone()
            if not row:
                raise Failure("HTTP_RECEIPT_NOT_RESERVED")
            if row[0] != "PENDING":
                if row[0] == "DONE" and row[1] == code and json.loads(row[2]) == data:
                    return
                raise Failure("HTTP_RECEIPT_UPDATE_CONFLICT")
            if code == 200 and isinstance(data, dict):
                preview = data.get("preview")
                if isinstance(preview, dict):
                    ident = preview.get("preview_id")
                    if not isinstance(ident, str):
                        raise Failure("PREVIEW_RESPONSE_INVALID")
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO draft_owners VALUES(?,?)", (ident, owner)
                    )
                    if self.store.db.execute(
                        "SELECT owner FROM draft_owners WHERE id=?", (ident,)
                    ).fetchone() != (owner,):
                        raise Failure("PREVIEW_CLIENT_MISMATCH")
                sid = data.get("session_id")
                if sid is not None:
                    if not isinstance(sid, str):
                        raise Failure("SESSION_RESPONSE_INVALID")
                    self.store.db.execute(
                        "INSERT OR IGNORE INTO session_owners VALUES(?,?)", (sid, owner)
                    )
                    if self.store.db.execute(
                        "SELECT owner FROM session_owners WHERE id=?", (sid,)
                    ).fetchone() != (owner,):
                        raise Failure("SESSION_CLIENT_MISMATCH")
                    body = self.store.get_session(sid)
                    if (
                        not data.get("has_preview")
                        and data.get("source_result") is not None
                        and not body.get("source_snapshot_id")
                    ):
                        self.live_snapshots.save(
                            owner, sid, data["revision"], data["request"], data["source_result"]
                        )
                        data.update(self.execute("read", sid=sid))
            self.store.db.execute(
                "UPDATE http_requests SET state='DONE',code=?,response=? WHERE id=? AND owner=? AND state='PENDING'",
                (code, json.dumps(data, ensure_ascii=False), rid, owner),
            )
            self.finish_http_metadata(rid, code, data)

    def complete_skill_run(self, run, out):
        # Skill trace, preview ownership and replay result share ONE local commit.
        # HTTP delivery can fail afterwards without losing the completed result.
        with self.lock, self.store.transaction():
            self.skill_agent.save(run)
            row = self.store.db.execute(
                "SELECT owner FROM http_requests WHERE id=?", (run["id"],)
            ).fetchone()
            if row:
                self.complete_http_result(row[0], run["id"], 200, out)
        ack = getattr(self.skill_transport, "acknowledge_run", None)
        if callable(ack):
            try:
                ack(run["id"])
            except (ValueError, __import__("sqlite3").Error):
                pass  # result is already durable

    def recovery_details(self, rid, owner, owner_in_flight, supplier_steps):
        with self.lock:
            row = self.store.db.execute(
                "SELECT state,record FROM skill_runs WHERE id=?", (rid,)
            ).fetchone()
        attempts = []
        known = True
        try:
            rows = getattr(self.skill_transport, "rows", lambda: [])()
            attempts = [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "route",
                        "provider",
                        "model",
                        "reported_model",
                        "state",
                        "http",
                        "usage",
                        "estimated_cost",
                        "unit",
                    )
                }
                for r in rows
                if isinstance(r.get("tag"), str) and r["tag"].split(":", 1)[0] == rid
            ]
        except (ValueError, __import__("sqlite3").Error):
            known = False
        # Never infer terminality from elapsed time, a missing UI marker or a restart.
        uncertain = any(
            r.get("state") in ("RESERVED", "UNKNOWN_OUTCOME") for r in attempts + supplier_steps
        )
        return {
            "trace_status": row[0] if row else None,
            "model_steps": attempts,
            "owner_in_flight": owner_in_flight,
            "safe_to_continue": bool(
                row and row[0] == "FAILED" and known and not uncertain and not owner_in_flight
            ),
            "redispatch": False,
        }

    def acknowledge_http(self, rid, owner):
        with self.lock, self.store.transaction():
            row = self.store.db.execute(
                "SELECT state FROM http_requests WHERE id=? AND owner=?", (rid, owner)
            ).fetchone()
            if not row or row[0] not in ("DONE", "BODY_EXPIRED"):
                raise Failure("HTTP_RESULT_NOT_ACKNOWLEDGEABLE")
            self.store.db.execute(
                "UPDATE http_request_meta SET ui_ack_at=COALESCE(ui_ack_at,?) WHERE id=?",
                (time.time(), rid),
            )
        return {"acknowledged": True, "redispatch": False}

    def dialogue_page(self, owner, kind, identifier, before=None):
        if kind not in ("sid", "draft_id"):
            raise Failure("HISTORY_SCOPE_INVALID")
        if before is not None and (type(before) is not int or before < 1):
            raise Failure("HISTORY_CURSOR_INVALID")
        with self.lock:
            rows = self.store.db.execute(
                "SELECT rowid,id,user_text,assistant_text,created_at FROM dialogue_history WHERE owner=? AND "
                + kind
                + "=? AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT 101",
                (owner, identifier, before, before),
            ).fetchall()
        more = len(rows) > 100
        rows = rows[:100]
        return {
            "turns": [
                {
                    "ordinal": r[0],
                    "request_id": r[1],
                    "user_text": r[2],
                    "assistant_text": r[3],
                    "created_at": r[4],
                }
                for r in reversed(rows)
            ],
            "before": rows[-1][0] if more else None,
            "retention_days": 30,
        }

    def cleanup_request_journal(self, at=None):
        at = time.time() if at is None else at
        if at - self._journal_cleanup_at < 60:
            return
        with self.lock, self.store.transaction():
            rows = self.store.db.execute(
                "SELECT h.id,h.code,m.body_sha256 FROM http_requests h JOIN http_request_meta m ON m.id=h.id WHERE h.state='DONE' AND m.completed_at IS NOT NULL AND (m.completed_at<? OR (m.ui_ack_at IS NOT NULL AND m.completed_at<?))",
                (at - 7 * 86400, at - 86400),
            ).fetchall()
            for rid, code, digest in rows:
                tombstone = {
                    "error": "RESPONSE_BODY_EXPIRED_NO_REPLAY",
                    "original_code": code,
                    "response_sha256": digest,
                    "redispatch": False,
                }
                self.store.db.execute(
                    "UPDATE http_requests SET state='BODY_EXPIRED',code=410,response=? WHERE id=?",
                    (canonical(tombstone), rid),
                )
            self.store.db.execute(
                "DELETE FROM dialogue_history WHERE created_at<?", (at - 30 * 86400,)
            )
            for rid, raw in self.store.db.execute(
                "SELECT id,record FROM skill_runs WHERE state='COMPLETE'"
            ).fetchall():
                trace = json.loads(raw)
                if trace.get("finished_at", at) > at - 7 * 86400 or trace.get("payload_state"):
                    continue
                for field in ("evidence", "intent", "jev", "comparison"):
                    if field in trace:
                        trace[field + "_sha256"] = hashlib.sha256(
                            canonical(trace.pop(field)).encode()
                        ).hexdigest()
                for step in trace.get("steps", []):
                    if "arguments" in step:
                        step["arguments_sha256"] = hashlib.sha256(
                            canonical(step.pop("arguments")).encode()
                        ).hexdigest()
                trace["payload_state"] = "EXPIRED_PROCESSED"
                self.store.db.execute(
                    "UPDATE skill_runs SET record=? WHERE id=?", (canonical(trace), rid)
                )
        self._journal_cleanup_at = at
