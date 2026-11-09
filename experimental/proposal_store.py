"""Atomic local journal for sessions and one-use, authenticated proposals."""

import sqlite3, json, secrets, hashlib, hmac, time
from contextlib import contextmanager
from travel_core import InputError
from session import Conflict


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ProposalStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, owner TEXT, revision INTEGER, kind TEXT NOT NULL,
          body TEXT NOT NULL, signature TEXT NOT NULL, expires REAL NOT NULL, lifecycle TEXT NOT NULL, result_sid TEXT);
        CREATE TABLE IF NOT EXISTS meta(name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, at REAL, sid TEXT, kind TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS draft_groups(id TEXT PRIMARY KEY, preview_id TEXT NOT NULL, seq INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS draft_latest_seq(id TEXT PRIMARY KEY, seq INTEGER NOT NULL CHECK(seq >= 0));
        """)
        try:
            self.db.execute("ALTER TABLE draft_groups ADD COLUMN seq INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self.db.execute(
            "INSERT OR IGNORE INTO meta VALUES (?,?)", ("proposal_key", secrets.token_hex(32))
        )
        self.key = bytes.fromhex(
            self.db.execute("SELECT value FROM meta WHERE name=?", ("proposal_key",)).fetchone()[0]
        )

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def event(self, sid, kind, body):
        self.db.execute(
            "INSERT INTO events(at,sid,kind,body) VALUES (?,?,?,?)",
            (time.time(), sid, kind, canonical(body)),
        )

    def put_session(self, sid, body):
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (sid, canonical(body)))

    def get_session(self, sid):
        row = self.db.execute("SELECT body FROM sessions WHERE id=?", (sid,)).fetchone()
        return json.loads(row[0]) if row else None

    def sign(self, body):
        return hmac.new(self.key, canonical(body).encode("utf-8"), hashlib.sha256).hexdigest()

    def create(self, owner, revision, kind, body, ttl=900):
        body = json.loads(canonical(body))
        identifier = secrets.token_hex(32)
        body.update(
            preview_id=identifier,
            owner_session=owner,
            source_revision=revision,
            action=kind,
            created_at=time.time(),
            expires_at=time.time() + ttl,
        )
        if owner:
            self.db.execute(
                "UPDATE proposals SET lifecycle='SUPERSEDED' WHERE owner=? AND lifecycle='PENDING'",
                (owner,),
            )
        self.db.execute(
            "INSERT INTO proposals VALUES (?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                owner,
                revision,
                kind,
                canonical(body),
                self.sign(body),
                body["expires_at"],
                "PENDING",
                None,
            ),
        )
        self.event(
            owner,
            "PREVIEW_" + kind.upper(),
            {"preview_id": identifier, "source_revision": revision},
        )
        return body

    def get(self, identifier, owner, revision=None):
        if (
            not isinstance(identifier, str)
            or len(identifier) != 64
            or any(c not in "0123456789abcdef" for c in identifier)
        ):
            raise InputError("INVALID_PREVIEW_ID")
        row = self.db.execute(
            "SELECT owner,revision,kind,body,signature,expires,lifecycle FROM proposals WHERE id=?",
            (identifier,),
        ).fetchone()
        if not row:
            raise InputError("PREVIEW_NOT_FOUND")
        if row[0] != owner:
            raise InputError("PREVIEW_OWNER_MISMATCH")
        if row[6] != "PENDING":
            raise Conflict("PREVIEW_" + row[6])
        if row[5] <= time.time():
            raise Conflict("PREVIEW_EXPIRED")
        body = json.loads(row[3])
        if not hmac.compare_digest(row[4], self.sign(body)):
            raise InputError("PREVIEW_TAMPERED")
        if body["expires_at"] != row[5]:
            raise InputError("PREVIEW_TAMPERED")
        if (
            body["owner_session"] != row[0]
            or body["source_revision"] != row[1]
            or body["action"] != row[2]
        ):
            raise InputError("PREVIEW_TAMPERED")
        if row[0] is not None and (type(revision) is not int or revision != row[1]):
            raise Conflict("STALE_REVISION")
        if row[0] is None and revision is not None:
            raise Conflict("STALE_REVISION")
        return body

    def finish(self, identifier, lifecycle, sid=None):
        changed = self.db.execute(
            "UPDATE proposals SET lifecycle=?,result_sid=? WHERE id=? AND lifecycle='PENDING'",
            (lifecycle, sid, identifier),
        ).rowcount
        if changed != 1:
            raise Conflict("PREVIEW_ALREADY_CONSUMED")
        self.event(sid, lifecycle, {"preview_id": identifier})

    def status(self, identifier, owner):
        if (
            not isinstance(identifier, str)
            or len(identifier) != 64
            or any(x not in "0123456789abcdef" for x in identifier)
        ):
            raise InputError("INVALID_PREVIEW_ID")
        row = self.db.execute(
            "SELECT owner,body,signature,lifecycle,result_sid,expires FROM proposals WHERE id=?",
            (identifier,),
        ).fetchone()
        if not row:
            raise InputError("PREVIEW_NOT_FOUND")
        if row[0] != owner:
            raise InputError("PREVIEW_OWNER_MISMATCH")
        body = json.loads(row[1])
        if not hmac.compare_digest(row[2], self.sign(body)) or row[5] != body["expires_at"]:
            raise InputError("PREVIEW_TAMPERED")
        lifecycle = "EXPIRED" if row[3] == "PENDING" and row[5] <= time.time() else row[3]
        return {
            "preview_id": identifier,
            "lifecycle": lifecycle,
            "result_session_id": row[4] if lifecycle == "COMMITTED" else None,
            "source_revision": body["source_revision"],
        }

    def invalidate(self, sid):
        self.db.execute(
            "UPDATE proposals SET lifecycle='STALE' WHERE owner=? AND lifecycle='PENDING'", (sid,)
        )

    def current(self, sid):
        row = self.db.execute(
            "SELECT id,revision FROM proposals WHERE owner=? AND lifecycle='PENDING' AND expires>? ORDER BY expires DESC LIMIT 1",
            (sid, time.time()),
        ).fetchone()
        if row:
            body = self.get(row[0], sid, row[1])
            body["has_preview"] = True
            return body
        return {"has_preview": False}
