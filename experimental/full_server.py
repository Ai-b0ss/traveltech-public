"""Full original UI plus session nonces, durable request replay and live read-only tools."""

import argparse, base64, binascii, io, json, hashlib, secrets, sqlite3, threading, re, sys, time
from pathlib import Path
import os
import signal
from urllib.parse import urlparse, parse_qs
from app import Application, request_from, Handler as LegacyHandler, ExclusiveHTTPServer
from model_interface import dispatch
from request_journal import RequestJournalMixin
from mock_cart import CartError, CartSnapshot, MockOrderStore
from trip_plan import demo_trip_plan
from provider_transport import CreditTransport, Failure, strict_json, canonical, now
from live_data import PublicData, DataError
from full_agent import FullAgent
from four_route_transport import FourRouteTransport, inkstone_routes
from logicc_transport import logicc_routes
from live_snapshot_store import LiveSnapshotStore
from skill_system import SkillAgent
from skill_system.backend import resolve_live_location
from skill_system.runtime import LocalSkillsTransport
from skill_system.transport import SkillTransport, PROFILES
from skill_system.safe_rotation import SafeRotationTransport
from skill_system.logicc_ladder import TravelModelLadder, MODELS, LOGICC_MODELS
from skill_system.jev import JevAdvisor

ROOT = Path(__file__).resolve().parents[1]


class FullApplication(RequestJournalMixin, Application):
    def __init__(self, storage, real=False, credit_state=None, transport=None, data=None):
        super().__init__(storage)
        self.real = real
        self.gate = threading.Lock()
        self.active_owners = set()
        self.active_scopes = {}
        self.transport = transport or (
            FourRouteTransport(credit_state or ROOT / "budget_state") if real else None
        )
        self.public = data or PublicData(self.storage / "public_data")
        self.full_agent = FullAgent(self, self.transport) if self.transport else None
        self.skill_transport = (
            TravelModelLadder(
                SafeRotationTransport(
                    SkillTransport(Path(credit_state or ROOT / "budget_state") / "skills")
                ),
                Path(credit_state or ROOT / "budget_state") / "skills_ladder",
            )
            if real
            else LocalSkillsTransport()
        )
        self.skill_agent = SkillAgent(
            self,
            self.skill_transport,
            advisor=JevAdvisor(self.skill_transport.jev) if real else JevAdvisor(),
        )
        self.store.db.executescript(
            "CREATE TABLE IF NOT EXISTS http_requests(id TEXT PRIMARY KEY,owner TEXT,fingerprint TEXT,state TEXT,code INTEGER,response TEXT); CREATE TABLE IF NOT EXISTS atomic_http_attempts(id TEXT PRIMARY KEY,kind TEXT NOT NULL); CREATE TABLE IF NOT EXISTS client_nonces(hash TEXT PRIMARY KEY); CREATE TABLE IF NOT EXISTS draft_owners(id TEXT PRIMARY KEY,owner TEXT NOT NULL); CREATE TABLE IF NOT EXISTS session_owners(id TEXT PRIMARY KEY,owner TEXT NOT NULL); CREATE TABLE IF NOT EXISTS mutation_limits(owner TEXT PRIMARY KEY,window_start REAL NOT NULL,consumed INTEGER NOT NULL);"
        )
        # Explicit migration: legacy nonce rows do NOT get newly assigned authority.
        # They have expires_at=0, so no unverified old credential can claim trips.
        nonce_cols = {row[1] for row in self.store.db.execute("PRAGMA table_info(client_nonces)")}
        for col, definition in (
            ("issued_at", "REAL NOT NULL DEFAULT 0"),
            ("expires_at", "REAL NOT NULL DEFAULT 0"),
            ("revoked_at", "REAL"),
        ):
            if col not in nonce_cols:
                self.store.db.execute(f"ALTER TABLE client_nonces ADD COLUMN {col} {definition}")
        self.nonce_idle_seconds = 14 * 86400
        self.nonce_mints_per_minute = 32
        self.mutations_per_minute = 24
        self.global_mutations_per_minute = 120
        self.live_snapshots = LiveSnapshotStore(self)
        self.mock_orders = MockOrderStore(self.storage / "mock_orders.sqlite")
        self.init_request_journal()
        from pilot_capture import PilotCapture
        self.pilot_capture = PilotCapture(self)

    def demo_http(self, owner, path, body, request_id, fingerprint):
        """Fixed MOCK only. Hold the session write lock through the local cart commit.

        MockOrderStore commits its order and idempotency receipt together. If the
        process dies before the HTTP journal commit, repeating the same cart/key
        reconstructs the same response, without any external operation.
        """
        required = {"sid", "revision"}
        if path == "/api/order/demo":
            required |= {"cart_id", "idempotency_key"}
        if set(body) != required:
            raise Failure("DEMO_FIELDS_NOT_ALLOWED")
        sid, revision = body["sid"], body["revision"]
        if not isinstance(sid, str) or not re.fullmatch("[a-f0-9]{32}", sid):
            raise Failure("DEMO_SESSION_REQUIRED")
        if type(revision) is not int or not 0 <= revision <= 2**53 - 1:
            raise Failure("DEMO_REVISION_INVALID")
        if self.real:
            raise Failure("OFFLINE_DEMO_ONLY")
        with self.lock, self.store.transaction():
            # Never import legacy files, infer ownership, or use submitted context.
            if self.store.db.execute(
                "SELECT owner FROM session_owners WHERE id=?", (sid,)
            ).fetchone() != (owner,):
                raise Failure("CAPABILITY_NOT_OWNED")
            saved = self.store.get_session(sid)
            if not saved or saved.get("schema") != 1:
                raise Failure("DEMO_SESSION_UNVERIFIED")
            if saved.get("source_mode") != "archive":
                raise Failure("OFFLINE_DEMO_ONLY")
            state = saved.get("state", {})
            current = state.get("revision")
            if type(current) is not int or revision != current:
                raise Failure("STALE_REVISION")
            if self.store.db.execute(
                "SELECT 1 FROM proposals WHERE owner=? AND lifecycle='PENDING' AND expires>?",
                (sid, time.time()),
            ).fetchone():
                raise Failure("DEMO_PREVIEW_PENDING")
            plan = demo_trip_plan()
            # The source is the fixed revision-0 example, never a refreshed quote.
            if current != plan["revision"]:
                raise Failure("STALE_REVISION")
            try:
                req = request_from(state["request"])
                from city_names import canonical_city

                matches = (
                    canonical_city(req.city, req.country)[:2] == ("Dubai", "AE")
                    and req.checkin == plan["start_date"]
                    and req.checkout == plan["end_date"]
                    and req.currency == plan["currency"]
                    and sum(room.adults for room in req.rooms) == plan["travellers"]["adults"]
                    and sorted(age for room in req.rooms for age in room.children)
                    == sorted(plan["travellers"]["children"])
                )
            except (ValueError, TypeError, KeyError, AttributeError):
                raise Failure("DEMO_SESSION_UNVERIFIED") from None
            if not matches:
                raise Failure("DEMO_CONTEXT_MISMATCH")
            if any(plan[name]["source_mode"] != "MOCK" for name in
                   ("hotel", "transport", "transfer", "insurance") if name in plan):
                raise Failure("DEMO_SOURCE_INVALID")
            # IDs bind the server's immutable example to this owner, sid and revision.
            cart_id = "demo-" + hashlib.sha256(canonical([owner, sid, current]).encode()).hexdigest()
            if path == "/api/order/demo" and body["cart_id"] != cart_id:
                raise Failure("DEMO_CART_MISMATCH")
            row = self.store.db.execute(
                "SELECT owner,fingerprint,state,code,response FROM http_requests WHERE id=?",
                (request_id,),
            ).fetchone()
            if row:
                if row[:2] != (owner, fingerprint):
                    raise Failure("REQUEST_ID_CONFLICT")
                if row[2] not in ("DONE", "BODY_EXPIRED"):
                    raise Failure("UNKNOWN_REQUEST_NO_RETRY")
                return row[3], json.loads(row[4])
            if not self.check_mutation_budget(owner):
                return 429, {"error": "MUTATION_RATE_LIMIT", "retry_after_seconds": 60}
            self.store.db.execute(
                "INSERT INTO http_requests VALUES(?,?,?,?,?,?)",
                (request_id, owner, fingerprint, "PENDING", None, None),
            )
            self.start_http_metadata(request_id, owner, path, body)
            try:
                if path == "/api/cart/demo":
                    snapshot = CartSnapshot(plan, owner=owner, cart_id=cart_id, expected_revision=current)
                    out = {"cart": self.mock_orders.save_cart(snapshot, expected_revision=current)}
                else:
                    out = self.mock_orders.process(owner=owner, cart_id=cart_id,
                        expected_revision=current, idempotency_key=body["idempotency_key"])
            except CartError as error:
                raise Failure(error.code) from error
            out.update(mode="DEMO_ONLY", verification="FIXED_MOCK_DEMO",
                notice="DEMO: money not charged; nothing booked. Fixed MOCK example, not a personal quote. Origin and budget unverified.")
            self.complete_http_result(owner, request_id, 200, out)
            return 200, out

    def session_plan_http(self, owner, body):
        """Authenticated read, revalidated on every request (including replays).

        Never journal/cache this read: a receipt cannot authorize an old revision.
        The ordinary mutation journal and fixed demo cart remain separate.
        """
        from trip_plan_session import read_session_trip_plan

        if set(body) - {"sid", "expected_revision"}:
            return 400, {"error": "PRICE_INJECTION_DENIED"}
        sid, revision = body.get("sid"), body.get("expected_revision")
        if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{32}", sid):
            return 400, {"error": "SESSION_ID_INVALID"}
        if type(revision) is not int or not 0 <= revision <= 2**53 - 1:
            return 400, {"error": "REVISION_REQUIRED"}
        with self.lock:
            code, out = read_session_trip_plan(self.store, owner, sid, expected_revision=revision)
            # Add observations only after the existing owner/revision boundary.
            # This read never dispatches Pilot and cannot promote A05 or a total.
            if "context" in out:
                from supplier_pilot import PilotError
                try:
                    out["pilot_observations"] = self.pilot_capture.read(owner, sid, revision)
                except PilotError:
                    out["pilot_observations"] = {"status": "CONTEXT_HOLD", "observations": []}
            return code, out

    def action_http_atomic(self, owner, request_id, command, before_receipt=None):
        # Internal archive-state commands have no outbound supplier/model side effects.
        # Their local mutation and HTTP journal completion must be one SQLite commit.
        if not isinstance(command, dict) or set(command) - {
            "action",
            "sid",
            "name",
            "budget",
            "revision",
            "preview_id",
        }:
            raise Failure("ACTION_FIELDS_NOT_ALLOWED")
        if command.get("action") == "budget":
            raise Failure("PREVIEW_CONFIRM_REQUIRED")
        with self.lock, self.store.transaction():
            row = self.store.db.execute(
                "SELECT owner,state FROM http_requests WHERE id=?", (request_id,)
            ).fetchone()
            if row != (owner, "PENDING"):
                raise Failure("HTTP_RECEIPT_NOT_RESERVED")
            sid = command.get("sid")
            if sid is not None:
                bound = self.store.db.execute(
                    "SELECT owner FROM session_owners WHERE id=?", (sid,)
                ).fetchone()
                if bound != (owner,):
                    raise Failure("SESSION_CLIENT_MISMATCH")
            preview_id = command.get("preview_id")
            if preview_id is not None:
                bound = self.store.db.execute(
                    "SELECT owner FROM draft_owners WHERE id=?", (preview_id,)
                ).fetchone()
                if bound != (owner,):
                    raise Failure("PREVIEW_CLIENT_MISMATCH")
            out = dispatch(self, command)
            result_sid = out.get("session_id") if isinstance(out, dict) else None
            if result_sid is not None:
                if not isinstance(result_sid, str):
                    raise Failure("ACTION_RESULT_SESSION_INVALID")
                self.store.db.execute(
                    "INSERT OR IGNORE INTO session_owners VALUES(?,?)", (result_sid, owner)
                )
                bound = self.store.db.execute(
                    "SELECT owner FROM session_owners WHERE id=?", (result_sid,)
                ).fetchone()
                if bound != (owner,):
                    raise Failure("SESSION_CLIENT_MISMATCH")
            if (
                result_sid is not None
                and out.get("source_result") is not None
                and command.get("action") == "undo"
            ):
                self.live_snapshots.save(
                    owner, result_sid, out["revision"], out["request"], out["source_result"]
                )
                out.update(self.execute("read", sid=result_sid))
            if before_receipt is not None:
                before_receipt()
            changed = self.store.db.execute(
                "UPDATE http_requests SET state='DONE',code=200,response=? WHERE id=? AND owner=? AND state='PENDING'",
                (json.dumps(out, ensure_ascii=False), request_id, owner),
            ).rowcount
            if changed != 1:
                raise Failure("HTTP_RECEIPT_UPDATE_CONFLICT")
            self.finish_http_metadata(request_id, 200, out)
            return out

    def commit_http_atomic(self, owner, request_id, sid, preview_id, revision, before_receipt=None):
        # One SQLite COMMIT protects BOTH domain state and the HTTP replay receipt.
        # A crash before SQLite commits rolls back the preview and the trip as well.
        with self.lock, self.store.transaction():
            row = self.store.db.execute(
                "SELECT owner,state FROM http_requests WHERE id=?", (request_id,)
            ).fetchone()
            if row != (owner, "PENDING"):
                raise Failure("HTTP_RECEIPT_NOT_RESERVED")
            preview_owner = self.store.db.execute(
                "SELECT owner FROM draft_owners WHERE id=?", (preview_id,)
            ).fetchone()
            if preview_owner != (owner,):
                raise Failure("PREVIEW_CLIENT_MISMATCH")
            if sid is not None:
                session_owner = self.store.db.execute(
                    "SELECT owner FROM session_owners WHERE id=?", (sid,)
                ).fetchone()
                if session_owner != (owner,):
                    raise Failure("SESSION_CLIENT_MISMATCH")
            out = super().commit_proposal(sid=sid, preview_id=preview_id, revision=revision)
            result_sid = out.get("session_id")
            if not isinstance(result_sid, str):
                raise Failure("COMMIT_RESULT_SESSION_INVALID")
            self.store.db.execute(
                "INSERT OR IGNORE INTO session_owners VALUES(?,?)", (result_sid, owner)
            )
            stored = self.store.db.execute(
                "SELECT owner FROM session_owners WHERE id=?", (result_sid,)
            ).fetchone()
            if stored != (owner,):
                raise Failure("SESSION_CLIENT_MISMATCH")
            if out.get("source_result") is not None:
                self.live_snapshots.save(
                    owner, result_sid, out["revision"], out["request"], out["source_result"]
                )
                out.update(self.execute("read", sid=result_sid))
            if before_receipt is not None:
                before_receipt()
            encoded = json.dumps(out, ensure_ascii=False)
            changed = self.store.db.execute(
                "UPDATE http_requests SET state='DONE',code=200,response=? WHERE id=? AND owner=? AND state='PENDING'",
                (encoded, request_id, owner),
            ).rowcount
            if changed != 1:
                raise Failure("HTTP_RECEIPT_UPDATE_CONFLICT")
            self.bind_dialogue(owner, preview_id, result_sid)
            self.finish_http_metadata(request_id, 200, out)
            return out

    def request_scopes(self, path, body):
        # Only supported commands with explicit, ownership-checked resources may
        # overlap. Owner-wide operations (including revocation and location cache)
        # still exclude all other mutations for that identity.
        if path not in ("/api/skills/agent", "/api/action", "/api/commit", "/api/budget-scope"):
            return frozenset(("*",))
        sid = body.get("sid")
        draft = body.get("draft_id")
        preview = body.get("preview_id")
        if preview is not None and sid is None:
            return frozenset(("*",))
        keys = set()
        if sid is not None:
            keys.add(("session", sid))
        if draft is not None:
            keys.add(("draft", draft))
        if preview is not None:
            keys.add(("preview", preview))
        return frozenset(keys or ("*",))

    def acquire_owner_gate(self, owner, scopes=None):
        scopes = scopes or frozenset(("*",))
        # Acquire every resource atomically; never hold this lock during inference.
        with self.gate:
            active = self.active_scopes.get(owner, set())
            if active and ("*" in active or "*" in scopes or active.intersection(scopes)):
                return False
            self.active_scopes.setdefault(owner, set()).update(scopes)
            self.active_owners.add(owner)
            return True

    def release_owner_gate(self, owner, scopes=None):
        scopes = scopes or frozenset(("*",))
        with self.gate:
            active = self.active_scopes.get(owner, set())
            active.difference_update(scopes)
            if not active:
                self.active_scopes.pop(owner, None)
                self.active_owners.discard(owner)

    def nonce(self, value=None):
        # Sliding inactivity expiry. Existing valid owners keep their own trip IDs;
        # expired/revoked/legacy tokens never regain the old identity.
        with self.lock, self.store.transaction():
            now_ts = time.time()
            if isinstance(value, str) and re.fullmatch("[A-Za-z0-9_-]{32,100}", value):
                digest = hashlib.sha256(value.encode()).hexdigest()
                row = self.store.db.execute(
                    "SELECT expires_at,revoked_at FROM client_nonces WHERE hash=?", (digest,)
                ).fetchone()
                if row and row[1] is None and row[0] > now_ts:
                    self.store.db.execute(
                        "UPDATE client_nonces SET expires_at=? WHERE hash=?",
                        (now_ts + self.nonce_idle_seconds, digest),
                    )
                    return value
            # Durable rolling one-minute issuance limit (including across restarts).
            # Prune long-dead bearer rows, but never orphan an active credential.
            self.store.db.execute(
                "DELETE FROM client_nonces WHERE issued_at<? AND "
                "(expires_at<? OR (revoked_at IS NOT NULL AND revoked_at<?))",
                (now_ts - 30 * 86400, now_ts - 30 * 86400, now_ts - 30 * 86400),
            )
            count = self.store.db.execute(
                "SELECT COUNT(*) FROM client_nonces WHERE issued_at>?", (now_ts - 60,)
            ).fetchone()[0]
            if count >= self.nonce_mints_per_minute:
                return None
            token = secrets.token_urlsafe(32)
            self.store.db.execute(
                "INSERT INTO client_nonces(hash,issued_at,expires_at,revoked_at) VALUES(?,?,?,NULL)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    now_ts,
                    now_ts + self.nonce_idle_seconds,
                ),
            )
            return token

    def nonce_owner(self, value):
        if not isinstance(value, str) or not re.fullmatch("[A-Za-z0-9_-]{32,100}", value):
            raise Failure("SESSION_NONCE_REQUIRED")
        digest = hashlib.sha256(value.encode()).hexdigest()
        with self.lock, self.store.transaction():
            now_ts = time.time()
            row = self.store.db.execute(
                "SELECT expires_at,revoked_at FROM client_nonces WHERE hash=?", (digest,)
            ).fetchone()
            if row is None or row[1] is not None or row[0] <= now_ts:
                raise Failure("SESSION_NONCE_EXPIRED_OR_REVOKED")
            self.store.db.execute(
                "UPDATE client_nonces SET expires_at=? WHERE hash=?",
                (now_ts + self.nonce_idle_seconds, digest),
            )
        return digest

    def revoke_nonce(self, digest):
        with self.lock, self.store.transaction():
            updated = self.store.db.execute(
                "UPDATE client_nonces SET revoked_at=? WHERE hash=? AND revoked_at IS NULL",
                (time.time(), digest),
            ).rowcount
            if updated != 1:
                raise Failure("SESSION_NONCE_EXPIRED_OR_REVOKED")

    def check_mutation_budget(self, digest):
        # Per-client AND global rate limits: minting a fresh nonce cannot sidestep
        # the aggregate bound. Both counters change inside one SQLite transaction.
        with self.lock, self.store.transaction():
            now_ts = time.time()
            checks = []
            for owner, ceiling in (
                (digest, self.mutations_per_minute),
                ("__GLOBAL_MUTATIONS__", self.global_mutations_per_minute),
            ):
                row = self.store.db.execute(
                    "SELECT window_start,consumed FROM mutation_limits WHERE owner=?", (owner,)
                ).fetchone()
                active = row is not None and now_ts - row[0] < 60
                if active and row[1] >= ceiling:
                    return False
                checks.append((owner, active))
            for owner, active in checks:
                if active:
                    self.store.db.execute(
                        "UPDATE mutation_limits SET consumed=consumed+1 WHERE owner=?", (owner,)
                    )
                else:
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO mutation_limits VALUES(?,?,?)", (owner, now_ts, 1)
                    )
            self.store.db.execute(
                "DELETE FROM mutation_limits WHERE window_start<?", (now_ts - 3600,)
            )
            return True


class PreviewAuthRequired(Failure):
    """HTTP Basic challenge required for an externally configured preview."""


class FullHandler(LegacyHandler):
    def preview_authorized(self):
        # Only the hash is stored. Password is a 256-bit random token issued
        # out-of-band and supplied by the browser over HTTPS.
        location = os.environ.get("TRAVELTECH_PREVIEW_AUTH_HASH_FILE")
        path = Path(location) if location else Path.home() / ".traveltech-secrets" / "preview_basic.sha256"
        try:
            digest = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            raise Failure("PREVIEW_AUTH_CONFIGURATION_MISSING")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise Failure("PREVIEW_AUTH_CONFIGURATION_INVALID")
        header = self.headers.get("Authorization", "")
        if not isinstance(header, str) or len(header) > 512 or not header.startswith("Basic "):
            raise PreviewAuthRequired("PREVIEW_BASIC_AUTH_REQUIRED")
        try:
            decoded = base64.b64decode(header[6:], validate=True)
        except (ValueError, binascii.Error):
            raise PreviewAuthRequired("PREVIEW_BASIC_AUTH_REQUIRED")
        if not decoded.startswith(b"traveltech:") or len(decoded) > 256:
            raise PreviewAuthRequired("PREVIEW_BASIC_AUTH_REQUIRED")
        actual = hashlib.sha256(decoded).hexdigest()
        if not secrets.compare_digest(actual, digest):
            raise PreviewAuthRequired("PREVIEW_BASIC_AUTH_REQUIRED")

    def local(self):
        # All routes, including /api/config and static assets, require HTTP
        # Basic when external preview is configured. Apply it also to the
        # localhost Host spelling: a public reverse proxy may forward spoofed
        # Host headers and its requests also appear to originate from loopback.
        preview_host = os.environ.get("TRAVELTECH_PREVIEW_HOST", "")
        host = self.headers.get("Host", "")
        if preview_host and host == preview_host:
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                raise Failure("PREVIEW_PEER_NOT_LOOPBACK")
            if self.headers.get("Origin") not in (None, "https://" + preview_host):
                raise Failure("PREVIEW_ORIGIN_NOT_ALLOWED")
        else:
            super().local()
        if preview_host:
            self.preview_authorized()

    def send_header(self, keyword, value):
        # Only the supported UI can display opt-in supplier photos. Preserve the
        # existing script/connect/frame restrictions and the retired handler policy.
        if keyword.lower() == "content-security-policy":
            value = value.replace("img-src 'none'", "img-src https://i.travelapi.com")
        return super().send_header(keyword, value)

    def end_headers(self):
        if getattr(self, "_send_retry_after", False):
            self.send_header("Retry-After", "60")
            self._send_retry_after = False
        if getattr(self, "_preview_auth_challenge", False):
            self.send_header("WWW-Authenticate", 'Basic realm="TravelTech private preview"')
            self._preview_auth_challenge = False
        return super().end_headers()

    def require_owned(self, kind, identifier):
        # Explicit bearer capability binding, not a guess based on a known ID.
        if kind not in ("session", "preview", "draft"):
            raise Failure("OWNERSHIP_KIND_INVALID")
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
            raise Failure("CAPABILITY_ID_REQUIRED")
        table = "session_owners" if kind == "session" else "draft_owners"
        with self.app.lock:
            record = self.app.store.db.execute(
                f"SELECT owner FROM {table} WHERE id=?", (identifier,)
            ).fetchone()
        if record is None or record[0] != self.owner:
            raise Failure("CAPABILITY_NOT_OWNED")

    def reply(self, code, data, kind="application/json; charset=utf-8"):
        self._preview_auth_challenge = code == 401
        self._send_retry_after = code == 429
        pending = getattr(self, "pending_request", None)
        if pending:
            self.app.complete_http_result(self.owner, pending, code, data)
            self.pending_request = None
        try:
            return super().reply(code, data, kind)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The durable result is already saved. A closing tab must not trigger a
            # second response or turn a completed operation into an unknown outcome.
            self.close_connection = True
            return None

    def owner_valid(self):
        token = self.headers.get("X-TravelTech-Nonce", "")
        self.owner = self.app.nonce_owner(token)
        return self.owner

    def do_GET(self):
        self.pending_request = None
        try:
            self.local()
            url = urlparse(self.path)
            if url.path == "/health":
                return self.reply(200, {"status": "ok"})
            if url.path == "/api/trip_plan/demo":
                from trip_plan import TripPlanError, demo_trip_plan

                try:
                    return self.reply(200, demo_trip_plan())
                except TripPlanError as error:
                    return self.reply(400, {"error": error.code})
            if url.path == "/api/trip_plan/session":
                return self.reply(405, {"error": "METHOD_NOT_ALLOWED"})
            if url.path == "/api/config":
                minted = self.app.nonce(self.headers.get("X-TravelTech-Nonce"))
                if minted is None:
                    return self.reply(
                        429, {"error": "NONCE_MINT_RATE_LIMIT", "retry_after_seconds": 60}
                    )
                return self.reply(
                    200,
                    {
                        "nonce": minted,
                        "mode": "REAL_CREDIT_ONLY" if self.app.real else "OFFLINE_FULL_UI",
                        "mvp_deterministic": os.environ.get("TRAVELTECH_MVP_DETERMINISTIC") == "1",
                        "hotel_source": "Configured primary supplier with explicit read-only fallback",
                        "primary": "Auto: Gemini 4 Argon -> InkStone when enabled; otherwise IISHKO -> InkStone",
                        "backup": "PRE_DISPATCH_ONLY_NO_SAME_REQUEST_REDISTRIBUTION",
                        "manual": "Gwarden_Gemini4Argon,IISHKO,InkStone_AgentsA1",
                        "model_route_status": self.app.skill_transport.route_status()
                        if hasattr(self.app.skill_transport, "route_status")
                        else {},
                        "additional_routes": {k: v for k, v in inkstone_routes().items() if k == "InkStone_AgentsA1"},
                        "skill_routes": {
                            "Auto": {"provider": "AUTO", "model": "SAFE_PRE_DISPATCH_ROTATION"},
                            **{
                                k: {"provider": v[0], "model": v[1]}
                                for k, v in PROFILES.items()
                                if k in ("Gwarden_Gemini4Argon", "IISHKO", "InkStone_AgentsA1")
                            },
                        },
                        "skills": self.app.skill_agent.registry.index(),
                        "jev": "OPTIONAL_SHADOW; OneAPI prior-outcome guard preserved",
                    },
                )
            if url.path == "/api/supplier/status":
                self.owner_valid()
                if not self.app.real:
                    return self.reply(
                        200,
                        {
                            "active_supplier": "OFFLINE",
                            "mode": "OFFLINE_ARCHIVE",
                            "live_search_enabled": False,
                            "booking_enabled": False,
                            "automatic_supplier_fallback": False,
                        },
                    )
                from supplier_router import public_supplier_status

                return self.reply(200, public_supplier_status())
            if url.path == "/trip_plan_demo.json":
                fixture = ROOT / "docs" / "contracts" / "trip_plan_v0.json"
                return self.reply(200, fixture.read_bytes(), "application/json; charset=utf-8")
            if url.path in (
                "/full.js", "/full.css", "/offer_sort.js", "/trip_plan_card.js",
                "/trip_plan_card.css", "/trip_plan_ui.js",
            ):
                f = ROOT / "experimental" / url.path[1:]
                return self.reply(
                    200,
                    f.read_bytes(),
                    "text/javascript; charset=utf-8"
                    if f.suffix == ".js"
                    else "text/css; charset=utf-8",
                )
            if url.path.startswith("/api/"):
                self.owner_valid()
                self.app.cleanup_request_journal()
                if url.path == "/api/dialogue/history":
                    args = parse_qs(url.query)
                    kind = "sid" if "sid" in args else "draft_id"
                    ident = args.get(kind, [None])[0]
                    self.require_owned("session" if kind == "sid" else "draft", ident)
                    before = args.get("before", [None])[0]
                    if before is not None and not re.fullmatch("[0-9]{1,12}", before):
                        raise Failure("HISTORY_CURSOR_INVALID")
                    return self.reply(
                        200,
                        self.app.dialogue_page(
                            self.owner, kind, ident, int(before) if before else None
                        ),
                    )
                if url.path == "/api/data/selection":
                    args = parse_qs(url.query)
                    sid = args.get("sid", [None])[0]
                    revision_text = args.get("revision", [None])[0]
                    if revision_text is None or not re.fullmatch("[0-9]{1,9}", revision_text):
                        raise Failure("REVISION_REQUIRED")
                    self.require_owned("session", sid)
                    return self.reply(
                        200, self.app.live_snapshots.selection(self.owner, sid, int(revision_text))
                    )
                if url.path == "/api/data/snapshot":
                    args = parse_qs(url.query)
                    sid = args.get("sid", [None])[0]
                    revision_text = args.get("revision", [None])[0]
                    if revision_text is None or not re.fullmatch("[0-9]{1,9}", revision_text):
                        raise Failure("REVISION_REQUIRED")
                    self.require_owned("session", sid)
                    snapshot_id = args.get("id", [None])[0]
                    return self.reply(
                        200,
                        self.app.live_snapshots.load(
                            self.owner, sid, int(revision_text), snapshot_id
                        ),
                    )
                if url.path == "/api/request/status":
                    identifier = parse_qs(url.query).get("id", [None])[0]
                    with self.app.gate:
                        owner_in_flight = self.owner in self.app.active_owners
                    with self.app.lock, self.app.store.transaction():
                        row = self.app.store.db.execute(
                            "SELECT owner,state,code,response FROM http_requests WHERE id=?",
                            (identifier,),
                        ).fetchone()
                        if not row or row[0] != self.owner:
                            return self.reply(404, {"error": "REQUEST_NOT_OWNED_OR_NOT_FOUND"})
                        marked = self.app.store.db.execute(
                            "SELECT kind FROM atomic_http_attempts WHERE id=?", (identifier,)
                        ).fetchone()
                        if (
                            row[1] == "PENDING"
                            and not owner_in_flight
                            and marked
                            and marked[0] in ("/api/action", "/api/commit")
                        ):
                            # For new atomic-only dispatches: domain and HTTP receipt share one COMMIT.
                            # This invariant is NEVER assumed for old, external, or unmarked requests.
                            evidence = {
                                "error": "ABORTED_LOCAL_ACTION_NO_EFFECT",
                                "effect": "NONE",
                                "automatic_retries": 0,
                            }
                            self.app.store.db.execute(
                                "UPDATE http_requests SET state='ABORTED_NO_EFFECT',code=409,response=? WHERE id=? AND owner=? AND state='PENDING'",
                                (json.dumps(evidence), identifier, self.owner),
                            )
                            row = (row[0], "ABORTED_NO_EFFECT", 409, json.dumps(evidence))
                    prior = json.loads(row[3]) if row[3] else None
                    proven_no_effect = row[1] == "ABORTED_NO_EFFECT" or (
                        row[1] == "DONE"
                        and row[2] != 200
                        and marked is not None
                        and isinstance(prior, dict)
                        and prior.get("state_changed") is False
                    )
                    supplier_dispatch = getattr(
                        getattr(self.app.public, "dispatch_journal", None), "rows_for_request", None
                    )
                    supplier_steps = (
                        supplier_dispatch(self.owner, identifier)
                        if callable(supplier_dispatch)
                        else []
                    )
                    recovery = self.app.recovery_details(
                        identifier, self.owner, owner_in_flight, supplier_steps
                    )
                    if (
                        row[1] == "BODY_EXPIRED"
                        and isinstance(prior, dict)
                        and prior.get("original_code") == 200
                        and not owner_in_flight
                    ):
                        recovery["safe_to_continue"] = True
                    # The HTTP owner was checked above; expose only bounded, non-payload
                    # progress from that exact run. No prompt, hotel text or credentials.
                    progress = None
                    with self.app.lock:
                        run_row = self.app.store.db.execute(
                            "SELECT record FROM skill_runs WHERE id=?", (identifier,)
                        ).fetchone()
                    if run_row:
                        run = strict_json(run_row[0])
                        stage = run.get("stage", "RUNNING")
                        allowed = {
                            "RUNNING",
                            "UNDERSTANDING",
                            "VALIDATING",
                            "SEARCHING",
                            "COMPARING",
                            "PREPARING_PREVIEW",
                            "INSPECTING",
                            "COMPLETE",
                            "FAILED",
                        }
                        progress = {
                            "stage": stage if stage in allowed else "RUNNING",
                            "elapsed_seconds": max(
                                0, int(time.time() - run.get("started_at", time.time()))
                            ),
                        }
                    return self.reply(
                        200,
                        {
                            "progress": progress,
                            "recovery": recovery,
                            "request_id": identifier,
                            "state": row[1],
                            "code": row[2],
                            "response": prior,
                            "effect": "NONE" if proven_no_effect else "UNKNOWN",
                            "supplier_steps": supplier_steps,
                            "redispatch": False,
                        },
                    )
                query = parse_qs(url.query)
                if url.path in ("/api/session", "/api/preview"):
                    self.require_owned("session", query.get("id", [None])[0])
                elif url.path in ("/api/proposal", "/api/proposal/status"):
                    self.require_owned("preview", query.get("id", [None])[0])
                    if query.get("sid", [None])[0] is not None:
                        self.require_owned("session", query.get("sid", [None])[0])
                elif url.path == "/api/live/latest":
                    if not self.app.live_enabled:
                        raise Failure("LIVE_TEST_DISABLED")
                    # Preserve the read-only latest-trip feature, but only within this nonce.
                    # A missing owner row means legacy data is NOT inherited by the caller.
                    with self.app.lock:
                        row = self.app.store.db.execute(
                            "SELECT p.result_sid FROM proposals p JOIN session_owners o ON o.id=p.result_sid "
                            "WHERE p.kind='create' AND p.lifecycle='COMMITTED' AND p.result_sid IS NOT NULL "
                            "AND o.owner=? ORDER BY p.rowid DESC LIMIT 1",
                            (self.owner,),
                        ).fetchone()
                        if not row:
                            return self.reply(404, {"error": "NO_LAB_TRIP_YET"})
                        return self.reply(200, self.app.execute("read", sid=row[0]))
            return super().do_GET()
        except PreviewAuthRequired:
            return self.reply(401, {"error": "PREVIEW_BASIC_AUTH_REQUIRED"})
        except (Failure, DataError, ValueError):
            # Reject invalid Host and preview Origin without dropping the
            # connection or accidentally exposing an internal exception.
            return self.reply(403, {"error": "SESSION_OR_PREVIEW_REFUSED"})

    def do_POST(self):
        self.pending_request = None
        gate = False
        gate_scopes = None
        self.effect_uncertain = False
        guest_token = None
        try:
            self.local()
            # Same hostname on local HTTP or Cloudflare HTTPS tunnel.
            # No wildcard, cross-origin, or proxy-provided arbitrary origins.
            host = self.headers.get("Host", "")
            if self.headers.get("Origin") not in ("http://" + host, "https://" + host):
                raise Failure("ORIGIN_REQUIRED")
            self.owner_valid()
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536 or self.headers.get("Content-Type") != "application/json":
                raise Failure("BODY_LIMIT_OR_TYPE")
            raw = self.rfile.read(size)
            body = strict_json(raw)
            if not isinstance(body, dict):
                raise Failure("BODY_SCHEMA")
            if "guest_nationality" in body and self.path in (
                "/api/skills/agent",
                "/api/data/search",
            ):
                from city_names import COUNTRIES
                from supplier_router import guest_nationality

                nation = body.pop("guest_nationality")
                if not isinstance(nation, str) or nation not in COUNTRIES:
                    raise Failure("GUEST_NATIONALITY_REQUIRED")
                guest_token = guest_nationality.set(nation)
            if (
                self.app.real
                and self.path in ("/api/skills/agent", "/api/full/agent")
                and body.get("provider") == "Pollinations"
            ):
                raise Failure("POLLINATIONS_DISABLED_IN_THIS_BUILD")
            rid = self.headers.get("X-Request-ID", "")
            if not re.fullmatch("[A-Za-z0-9_-]{16,100}", rid):
                raise Failure("REQUEST_ID_REQUIRED")
            if (
                self.app.real
                and self.path == "/api/skills/agent"
                and body.get("source_mode") == "live"
            ):
                # LiteAPI rates depend on the guest's nationality. Refuse the
                # HTTP operation before reserving its ID or dispatching any
                # billable model inference when the request lacks that field.
                from supplier_router import active_supplier, nationality

                if active_supplier() == "LITEAPI" and not nationality():
                    raise Failure("GUEST_NATIONALITY_REQUIRED")
            if self.path == "/api/request/ack":
                if set(body) != {"id"} or not isinstance(body["id"], str):
                    raise Failure("ACK_FIELDS_INVALID")
                return self.reply(200, self.app.acknowledge_http(body["id"], self.owner))
            if self.path == "/api/trip_plan/session":
                # Nonce, same-origin JSON and request ID were checked above.
                # Bypass cached mutation receipts so ownership/revision are fresh.
                code, out = self.app.session_plan_http(self.owner, body)
                return self.reply(code, out)
            if self.path in ("/api/cart/demo", "/api/order/demo"):
                # A dedicated local transaction validates current state even on replay.
                gate_scopes = frozenset(("*",))
                if not self.app.acquire_owner_gate(self.owner, gate_scopes):
                    return self.reply(409, {"error": "REQUEST_PENDING"})
                gate = True
                fp = hashlib.sha256(self.path.encode() + raw).hexdigest()
                # Unexpected failures may follow the separate durable mock commit.
                self.effect_uncertain = True
                try:
                    code, out = self.app.demo_http(self.owner, self.path, body, rid, fp)
                except Failure as error:
                    self.effect_uncertain = str(error) == "HTTP_RECEIPT_UPDATE_CONFLICT"
                    raise
                return self.reply(code, out)
            self.app.cleanup_request_journal()
            fp = hashlib.sha256(self.path.encode() + raw).hexdigest()
            with self.app.lock:
                row = self.app.store.db.execute(
                    "SELECT owner,fingerprint,state,code,response FROM http_requests WHERE id=?",
                    (rid,),
                ).fetchone()
                if row:
                    if row[0] != self.owner or row[1] != fp:
                        return self.reply(409, {"error": "REQUEST_ID_CONFLICT"})
                    if row[2] == "PENDING":
                        return self.reply(409, {"error": "UNKNOWN_REQUEST_NO_RETRY"})
                    return self.reply(row[3], json.loads(row[4]))
                # Read-only /api/sessions does not consume a mutation budget.
                if self.path != "/api/sessions" and not self.app.check_mutation_budget(self.owner):
                    return self.reply(
                        429, {"error": "MUTATION_RATE_LIMIT", "retry_after_seconds": 60}
                    )
                # Central check BEFORE any dispatch, mutation, preview, model call or data read.
                sid = body.get("sid")
                if sid is not None:
                    self.require_owned("session", sid)
                preview_id = body.get("preview_id")
                if preview_id is not None:
                    self.require_owned("preview", preview_id)
                draft_id = body.get("draft_id")
                if draft_id is not None:
                    if not isinstance(draft_id, str) or not 1 <= len(draft_id) <= 128:
                        raise Failure("DRAFT_ID_INVALID")
                    row = self.app.store.db.execute(
                        "SELECT owner FROM draft_owners WHERE id=?", (draft_id,)
                    ).fetchone()
                    if row and row[0] != self.owner:
                        raise Failure("DRAFT_CLIENT_MISMATCH")
                    self.app.store.db.execute(
                        "INSERT OR IGNORE INTO draft_owners VALUES(?,?)", (draft_id, self.owner)
                    )
                if self.path == "/api/sessions":
                    if (
                        set(body) != {"ids"}
                        or not isinstance(body["ids"], list)
                        or len(body["ids"]) > 100
                    ):
                        raise Failure("SESSION_LIST_SCHEMA")
                    for item in body["ids"]:
                        self.require_owned("session", item)
            # Listing owned capability IDs is a read; it must not contend for the mutation/dispatch gate.
            if self.path == "/api/sessions":
                if set(body) != {"ids"}:
                    raise Failure("SESSION_LIST_SCHEMA")
                return self.reply(200, self.app.list_sessions(body["ids"]))
            gate_scopes = self.app.request_scopes(self.path, body)
            if not self.app.acquire_owner_gate(self.owner, gate_scopes):
                return self.reply(409, {"error": "REQUEST_PENDING"})
            gate = True
            with self.app.lock, self.app.store.transaction():
                # Another resource/owner can race the first replay lookup. Recheck inside
                # the same transaction as reservation, before any external effect.
                row = self.app.store.db.execute(
                    "SELECT owner,fingerprint,state,code,response FROM http_requests WHERE id=?",
                    (rid,),
                ).fetchone()
                if row:
                    if row[0] != self.owner or row[1] != fp:
                        return self.reply(409, {"error": "REQUEST_ID_CONFLICT"})
                    if row[2] == "PENDING":
                        return self.reply(409, {"error": "UNKNOWN_REQUEST_NO_RETRY"})
                    return self.reply(row[3], json.loads(row[4]))
                self.app.store.db.execute(
                    "INSERT INTO http_requests VALUES(?,?,?,?,?,?)",
                    (rid, self.owner, fp, "PENDING", None, None),
                )
                self.app.start_http_metadata(rid, self.owner, self.path, body)
                if self.path in ("/api/action", "/api/commit"):
                    self.app.store.db.execute(
                        "INSERT INTO atomic_http_attempts(id,kind) VALUES(?,?)", (rid, self.path)
                    )
            self.pending_request = rid
            supplier_scope = getattr(self.app.public, "request_scope", None)
            if callable(supplier_scope):
                self.supplier_request_scope = supplier_scope(self.owner, rid)
                self.supplier_request_scope.__enter__()
            if self.path == "/api/action":
                out = self.app.action_http_atomic(self.owner, rid, body)
                self.pending_request = None
                return self.reply(200, out)
            if self.path == "/api/commit":
                if set(body) - {"sid", "preview_id", "revision"}:
                    raise Failure("COMMIT_FIELDS_NOT_ALLOWED")
                from session import Conflict

                try:
                    out = self.app.commit_http_atomic(
                        self.owner,
                        rid,
                        sid=body.get("sid"),
                        preview_id=body.get("preview_id"),
                        revision=body.get("revision"),
                    )
                except Conflict as error:
                    if str(error) != "STALE_REVISION":
                        raise
                    raise Failure("STALE_REVISION") from error
                self.pending_request = None
                return self.reply(200, out)
            if self.path == "/api/budget-scope":
                if set(body) != {
                    "action",
                    "sid",
                    "revision",
                    "amount",
                    "currency",
                    "confirmation",
                }:
                    raise Failure("BUDGET_SCOPE_FIELDS_NOT_ALLOWED")
                if (
                    body.get("action") != "confirm_separate_whole_trip"
                    or body.get("confirmation") != "SEPARATE_WHOLE_TRIP"
                ):
                    raise Failure("WHOLE_TRIP_CONFIRMATION_REQUIRED")
                if type(body.get("revision")) is not int:
                    raise Failure("STALE_REVISION")
                from session import Conflict
                from travel_core import InputError

                try:
                    out = self.app.confirm_separate_whole_trip(
                        sid=body["sid"],
                        revision=body["revision"],
                        amount=body["amount"],
                        currency=body["currency"],
                        confirmation=body["confirmation"],
                    )
                except Conflict as error:
                    label = str(error)
                    raise Failure(label if label == "STALE_REVISION" else "BUDGET_SCOPE_CONFLICT") from error
                except InputError as error:
                    label = str(error)
                    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,80}", label):
                        label = "BUDGET_SCOPE_INVALID"
                    raise Failure(label) from error
                return self.reply(200, out)
            if self.path == "/api/session/revoke":
                if body:
                    raise Failure("REVOKE_FIELDS_NOT_ALLOWED")
                self.app.revoke_nonce(self.owner)
                return self.reply(200, {"revoked": True, "message": "SESSION_NONCE_REVOKED"})
            if self.path == "/api/skills/agent":
                if set(body) - {
                    "text",
                    "provider",
                    "sid",
                    "revision",
                    "draft_id",
                    "source_mode",
                    "use_jev",
                }:
                    raise Failure("SKILL_FIELDS")
                if not self.app.real and body.get("source_mode", "archive") != "archive":
                    raise Failure("OFFLINE_NETWORK_DISABLED")
                if not self.app.real:
                    body["provider"] = "Local"
                    body["use_jev"] = False
                self.effect_uncertain = True
                return self.reply(200, self.app.skill_agent.run(**body, request_id=rid))
            if self.path == "/api/full/agent":
                if not self.app.real or not self.app.full_agent:
                    raise Failure("REAL_MODE_NOT_ENABLED")
                if set(body) - {"text", "provider", "sid", "revision", "draft_id", "failover"}:
                    raise Failure("AGENT_FIELDS")
                self.effect_uncertain = True
                return self.reply(200, self.app.full_agent.run(**body, request_id=rid))
            if self.path.startswith("/api/data/"):
                if not self.app.real:
                    raise Failure("OFFLINE_NETWORK_DISABLED")
                self.effect_uncertain = True
                operation = self.path.rsplit("/", 1)[-1]
                api = self.app.public
                if operation == "select":
                    if set(body) != {"sid", "revision", "source_snapshot_id", "offer_index"}:
                        raise Failure("SELECT_OFFER_SCHEMA")
                    return self.reply(
                        200,
                        self.app.live_snapshots.choose(
                            self.owner,
                            body["sid"],
                            body["revision"],
                            body["source_snapshot_id"],
                            body["offer_index"],
                        ),
                    )
                if operation == "accept_quote":
                    if set(body) != {
                        "sid",
                        "revision",
                        "source_snapshot_id",
                        "offer_index",
                        "check_id",
                    }:
                        raise Failure("QUOTE_ACCEPTANCE_SCHEMA")
                    return self.reply(
                        200,
                        self.app.live_snapshots.accept_quote(
                            self.owner,
                            body["sid"],
                            body["revision"],
                            body["source_snapshot_id"],
                            body["offer_index"],
                            body["check_id"],
                        ),
                    )
                if operation == "location":
                    result = api.location(**body)
                    (self.app.storage / ("location_" + self.owner + ".json")).write_text(
                        json.dumps(
                            {"keyword": body["keyword"], "result": result}, ensure_ascii=False
                        ),
                        encoding="utf-8",
                    )
                elif operation == "geo":
                    result = api.geo(**body)
                elif operation == "weather":
                    result = api.weather(**body)
                elif operation == "search":
                    if set(body) != {"sid", "revision", "location"}:
                        raise Failure("SEARCH_SCHEMA")
                    with self.app.lock:
                        trip = self.app.execute("read", sid=body["sid"])
                        if body["revision"] != trip["revision"]:
                            raise Failure("STALE_REVISION")
                    from supplier_router import active_supplier

                    if active_supplier() == "LITEAPI":
                        result = api.search(trip["request"], None)
                    else:
                        resolution = json.loads(
                            (self.app.storage / ("location_" + self.owner + ".json")).read_text(
                                encoding="utf-8"
                            )
                        )
                        loc = body["location"]
                        saved = resolution["result"]
                        if trip["request"]["country"] not in ("RU", "AE", "TR"):
                            # The supplier's exact City and ISO country binding is authoritative.
                            expected, _ = resolve_live_location(
                                request_from(trip["request"]), saved
                            )
                            if loc != expected:
                                raise Failure("TRIP_LOCATION_MISMATCH")
                        else:
                            city = trip["request"]["city"].casefold()
                            aliases = {
                                "dubai": ["dubai", "дубай", "دبي", "迪拜", "杜拜"],
                                "istanbul": ["istanbul", "стамбул", "伊斯坦布尔"],
                                "сочи": ["сочи", "sochi", "索契"],
                            }.get(city, [city])
                            countries = {
                                "AE": ["united arab emirates", "阿拉伯联合酋长国"],
                                "TR": ["turkey", "türkiye", "土耳其"],
                                "RU": ["russia", "russian federation", "俄罗斯"],
                            }.get(trip["request"]["country"], [])
                            if loc.get("mode") == "region":
                                region = next(
                                    (
                                        x
                                        for x in saved["regions"]
                                        if str(x["region_id"]) == loc.get("region_id")
                                        and (x.get("full_name") or x.get("name"))
                                        == loc.get("location_name")
                                    ),
                                    None,
                                )
                                if (
                                    not region
                                    or not any(
                                        a
                                        in (
                                            region.get("full_name") or region.get("name", "")
                                        ).casefold()
                                        for a in aliases
                                    )
                                    or region.get("country", "").casefold() not in countries
                                ):
                                    raise Failure("TRIP_LOCATION_MISMATCH")
                            else:
                                point = saved.get("place") or {}
                                address = point.get("formatted_address", "").casefold()
                                if (
                                    not point
                                    or any(
                                        loc.get(k) != point.get(k)
                                        for k in ("latitude", "longitude")
                                    )
                                    or loc.get("location_name") != point.get("name")
                                    or not any(a in address for a in aliases)
                                    or not any(c in address for c in countries)
                                ):
                                    raise Failure("TRIP_POINT_MISMATCH")
                        result = api.search(trip["request"], body["location"])
                    enrich = getattr(api, "enrich_details", None)
                    if callable(enrich):
                        result = enrich(result, {})
                    result = self.app.live_snapshots.save(
                        self.owner, body["sid"], body["revision"], trip["request"], result
                    )
                    file = self.app.storage / ("live_" + self.owner + ".json")
                    file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                elif operation == "availability":
                    if (
                        set(body) != {"offer_index", "sid", "revision", "source_snapshot_id"}
                        or type(body["offer_index"]) is not int
                    ):
                        raise Failure("AVAILABILITY_SCHEMA")
                    result = self.app.live_snapshots.load(
                        self.owner, body["sid"], body["revision"], body["source_snapshot_id"]
                    )
                    if result["snapshot_stale"]:
                        raise Failure("STALE_SOURCE_SNAPSHOT_RESEARCH_AGAIN")
                    with self.app.lock:
                        if (
                            self.app.execute("read", sid=body["sid"])["revision"]
                            != body["revision"]
                        ):
                            raise Failure("STALE_REVISION")
                    if not 0 <= body["offer_index"] < len(result["offers"]):
                        raise Failure("OFFER_INDEX")
                    result = api.availability(
                        result["offers"][body["offer_index"]], result["request"]
                    )
                    result = self.app.live_snapshots.record_check(
                        self.owner,
                        body["sid"],
                        body["revision"],
                        body["source_snapshot_id"],
                        body["offer_index"],
                        result,
                    )
                else:
                    raise Failure("READONLY_ENDPOINT_REFUSED")
                return self.reply(200, result)
            self.effect_uncertain = True
            self.rfile = io.BytesIO(raw)
            return super().do_POST()
        except PreviewAuthRequired:
            return self.reply(
                401,
                {"error": "PREVIEW_BASIC_AUTH_REQUIRED", "state_changed": False, "automatic_retries": 0},
            )
        except (
            Failure,
            DataError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            FileNotFoundError,
        ) as e:
            label = str(e)
            # Only stable machine codes cross the HTTP/journal boundary. Exception
            # details can contain authorization headers, paths or supplier payloads.
            if not re.fullmatch("[A-Z][A-Z0-9_]{0,119}", label):
                label = "REQUEST_REFUSED_DETAILS_REDACTED"
            denied = isinstance(e, Failure) and (
                label.startswith("SESSION_NONCE_")
                or label
                in (
                    "CAPABILITY_NOT_OWNED",
                    "SESSION_CLIENT_MISMATCH",
                    "PREVIEW_CLIENT_MISMATCH",
                    "DRAFT_CLIENT_MISMATCH",
                    "SESSION_NONCE_REQUIRED",
                )
            )
            supplier_issue = isinstance(e, DataError) and (
                label.startswith("SUPPLIER_") or label.startswith("UPSTREAM_")
            )
            status = (
                504
                if label == "REQUEST_DEADLINE_EXCEEDED"
                else 403
                if denied
                else 502
                if supplier_issue
                else 400
                if isinstance(e, (DataError, ValueError))
                else 502
            )
            return self.reply(
                status,
                {
                    "error": label
                    if isinstance(e, (Failure, DataError))
                    else "INPUT_OR_UPSTREAM_INVALID",
                    "state_changed": "UNKNOWN"
                    if getattr(self, "effect_uncertain", False)
                    else False,
                    "automatic_retries": 0,
                },
            )
        except Exception:
            return self.reply(
                502,
                {
                    "error": "INTERNAL_FAILURE_UNKNOWN_NO_RETRY",
                    "automatic_retries": 0,
                    "state_changed": "UNKNOWN"
                    if getattr(self, "effect_uncertain", False)
                    else False,
                },
            )
        finally:
            if guest_token is not None:
                from supplier_router import guest_nationality

                guest_nationality.reset(guest_token)
            supplier_context = getattr(self, "supplier_request_scope", None)
            if supplier_context is not None:
                supplier_context.__exit__(None, None, None)
                self.supplier_request_scope = None
            if gate:
                self.app.release_owner_gate(self.owner, gate_scopes)


def create_server(port=0, storage=None, real=False, credit_state=None, transport=None, data=None, host="127.0.0.1"):
    app = FullApplication(storage or ROOT / "runtime", real, credit_state, transport, data)

    class BoundHandler(FullHandler):
        pass

    BoundHandler.app = app
    try:
        server = ExclusiveHTTPServer((host, port), BoundHandler)
    except BaseException:
        app.close()
        raise
    return server, app


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--real", action="store_true")
    p.add_argument("--port", type=int, default=8815)
    p.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "0.0.0.0"))
    p.add_argument("--storage", type=Path)
    p.add_argument("--credit-state", type=Path)
    p.add_argument("--smoke", action="store_true")
    args = p.parse_args()
    try:
        server, app = create_server(args.port, args.storage, args.real, args.credit_state, host=args.host)
    except OSError:
        if args.host != "127.0.0.1":
            raise
        server, app = create_server(0, args.storage, args.real, args.credit_state, host=args.host)
    print(
        f"TravelTech full UI: http://{args.host}:{server.server_port} mode={'REAL_CREDIT_ONLY' if args.real else 'OFFLINE'}",
        flush=True,
    )
    if args.smoke:
        import urllib.error, urllib.request, uuid

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        # Ephemeral loopback port. This does not prove published container port 8080.
        scenarios = (("ru", "Сочи"), ("dubai", "Dubai"), ("istanbul", "Istanbul"))
        try:
            with urllib.request.urlopen(url + "/health", timeout=5) as response:
                health_code = response.status
                health = json.load(response)
            if health_code != 200 or health != {"status": "ok"}:
                print("SMOKE_HEALTH_FAIL", flush=True)
                raise SystemExit(1)
            for name, city in scenarios:
                with urllib.request.urlopen(url + "/api/config", timeout=5) as response:
                    config = json.load(response)
                payload = json.dumps({"action": "start", "name": name}).encode()
                request = urllib.request.Request(
                    url + "/api/action",
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Origin": url,
                        "X-TravelTech-Nonce": config["nonce"],
                        "X-Request-ID": uuid.uuid4().hex,
                    },
                )
                try:
                    with urllib.request.urlopen(request, timeout=20) as response:
                        trip = json.load(response)
                        code = response.status
                except urllib.error.HTTPError as error:
                    print(
                        f"SMOKE_HTTP_ERROR {name} {error.code}: {error.read().decode('utf-8', 'replace')[:200]}",
                        flush=True,
                    )
                    raise SystemExit(1) from error
                request_body = trip.get("request") if isinstance(trip, dict) else None
                cards = trip.get("cards") if isinstance(trip, dict) else None
                got = request_body.get("city") if isinstance(request_body, dict) else None
                if code != 200 or not isinstance(cards, list) or len(cards) != 3 or got != city:
                    print(f"SMOKE_SCENARIO_FAIL {name}", flush=True)
                    raise SystemExit(1)
            print("FULL_UI_BACKEND_SMOKE_PASS; INFERENCE_CALLS=0; PUBLIC_API_CALLS=0; HEALTH_JSON_OK=1", flush=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            app.close()
        sys.exit(0)
    def stop_on_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
