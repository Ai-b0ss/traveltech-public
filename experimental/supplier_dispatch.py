"""Durable pre-dispatch guard for READ-ONLY supplier HTTP calls.

Never performs a retry, never stores provider payloads, tokens or login data.
A crash leaves RESERVED, which is treated as an unknown outbound outcome.
"""

import hashlib
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from provider_transport import canonical


class SupplierDispatchJournal:
    def __init__(self, directory):
        self.path = Path(directory) / "supplier_dispatch.sqlite"
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
              CREATE TABLE IF NOT EXISTS supplier_dispatch(
                id TEXT PRIMARY KEY, owner_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL, step INTEGER NOT NULL,
                kind TEXT NOT NULL, payload_hash TEXT NOT NULL,
                state TEXT NOT NULL, started_at REAL NOT NULL,
                completed_at REAL, response_hash TEXT, error_type TEXT,
                UNIQUE(request_hash,step));
              CREATE INDEX IF NOT EXISTS supplier_unknown_lookup
                ON supplier_dispatch(owner_hash,kind,payload_hash,state);
            """)

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode("utf8")).hexdigest()

    def reserve(self, owner, request_id, step, kind, payload_digest):
        owner_key = self.digest(owner)
        request_key = self.digest(request_id)
        identifier = self.digest(request_key + ":" + str(step))
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute(
                    "SELECT 1 FROM supplier_dispatch WHERE request_hash=? AND step=?",
                    (request_key, step),
                ).fetchone():
                    raise ValueError("SUPPLIER_STEP_ALREADY_DISPATCHED_NO_RETRY")
                if db.execute(
                    """SELECT 1 FROM supplier_dispatch
                        WHERE owner_hash=? AND kind=? AND payload_hash=?
                        AND state IN ('RESERVED','UNKNOWN_OUTCOME') LIMIT 1""",
                    (owner_key, kind, payload_digest),
                ).fetchone():
                    raise ValueError("SUPPLIER_PRIOR_UNKNOWN_NO_RETRY")
                db.execute(
                    """INSERT INTO supplier_dispatch
                    (id,owner_hash,request_hash,step,kind,payload_hash,state,started_at)
                    VALUES(?,?,?,?,?,?,'RESERVED',?)""",
                    (identifier, owner_key, request_key, step, kind, payload_digest, time.time()),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return identifier

    def finish(self, identifier, success, result=None, error_type=None, *, dispatched=True):
        safe_error = (
            error_type
            if isinstance(error_type, str) and error_type.isidentifier()
            else "UPSTREAM_ERROR"
        )
        response_digest = self.digest(canonical(result)) if success and result is not None else None
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            try:
                updated = db.execute(
                    """UPDATE supplier_dispatch
                    SET state=?,completed_at=?,response_hash=?,error_type=?
                    WHERE id=? AND state='RESERVED'""",
                    (
                        "RESPONSE_RECEIVED"
                        if success
                        else "UNKNOWN_OUTCOME"
                        if dispatched
                        else "LOCAL_FAILURE",
                        time.time(),
                        response_digest,
                        None if success else safe_error,
                        identifier,
                    ),
                ).rowcount
                if updated != 1:
                    raise ValueError("SUPPLIER_LEDGER_CONFLICT")
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def rows_for_request(self, owner, request_id):
        owner_key = self.digest(owner)
        request_key = self.digest(request_id)
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            rows = db.execute(
                """SELECT step,kind,state,started_at,completed_at
                FROM supplier_dispatch WHERE owner_hash=? AND request_hash=?
                ORDER BY step LIMIT 40""",
                (owner_key, request_key),
            ).fetchall()
        # Never return owner hashes, request content, payment details or raw receipts.
        return [
            {
                "step": row[0],
                "kind": row[1],
                "state": row[2],
                "started_at": row[3],
                "completed_at": row[4],
            }
            for row in rows
        ]
