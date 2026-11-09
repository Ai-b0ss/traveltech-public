"""Bounded provider transport for opt-in synthetic TravelTech live tests.

OFFLINE_DEMO never instantiates this class. One request is reserved before dispatch.
A timeout is an UNKNOWN_OUTCOME, not a proof of failed inference. Future
independent, non-mutating proposals may use a different route subject to budget.
"""

import json, sqlite3, time, hashlib, urllib.request, urllib.error, secrets, threading
from urllib.parse import urlsplit
from decimal import Decimal
from pathlib import Path
from secret_reader import read_key
from ai_router import AdapterError

ENDPOINTS = {
    "https://api.oneapi.id/v1/chat/completions": ("OneAPI", 5, "oneapi.key"),
    "https://api.reformboss.com/v1/chat/completions": ("Reformboss", 5, "reformboss.key"),
    "https://api.oneapi.id/v1/systemone": ("Jev", 3, "oneapi.key"),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise AdapterError("GATEWAY_REDIRECT_REJECTED")


class GatewayTransport:
    def __init__(self, grant, ledger, opener=None, timeout_seconds=65):
        self.grant = grant
        self.path = Path(ledger)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.opener = opener
        if type(timeout_seconds) not in (int, float) or not 15 <= timeout_seconds <= 120:
            raise AdapterError("GATEWAY_TIMEOUT_INVALID")
        self.timeout_seconds = timeout_seconds
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db_lock = threading.RLock()
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS calls("
            "id TEXT PRIMARY KEY,provider TEXT,model TEXT,reserved TEXT,"
            "status TEXT,digest TEXT)"
        )
        # API-host cooldown outlives worker restarts and covers models sharing
        # the same upstream (e.g. OneAPI chat and its Jev endpoint).
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS provider_backoff("
            "host TEXT PRIMARY KEY,until_unix REAL NOT NULL,"
            "status INTEGER NOT NULL,recorded_at REAL NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS model_responses("
            "call_id TEXT PRIMARY KEY, sealed_response BLOB NOT NULL,"
            "response_sha256 TEXT NOT NULL, saved_unix REAL NOT NULL,"
            "app_acknowledged_unix REAL)"
        )
        # Migrate RC13 databases without losing their old encrypted responses.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(model_responses)")}
        if "app_acknowledged_unix" not in columns:
            self.db.execute("ALTER TABLE model_responses ADD COLUMN app_acknowledged_unix REAL")
        self.db.execute("PRAGMA secure_delete=ON")
        # Unlike encrypted bodies, processing evidence must outlive the TTL.
        # Only opaque call identifiers/timestamps are retained here.
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS model_processing_receipts("
            "call_id TEXT PRIMARY KEY, processed_unix REAL NOT NULL)"
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO model_processing_receipts(call_id,processed_unix) "
                "SELECT call_id,app_acknowledged_unix FROM model_responses "
                "WHERE app_acknowledged_unix IS NOT NULL"
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self._recovery = threading.local()
        self._housekeeping_lock = threading.RLock()
        self._housekeeping_stop = threading.Event()
        self._housekeeping_thread = None
        self.purge_expired_responses()

    def start_housekeeping(self, interval_seconds=30):
        # Start only after a ciphertext has been saved; no API polling.
        if type(interval_seconds) not in (int, float) or not 0.01 <= interval_seconds <= 3600:
            raise AdapterError("RESPONSE_CACHE_HOUSEKEEPING_INTERVAL_INVALID")
        with self._housekeeping_lock:
            previous = self._housekeeping_thread
            if previous is not None and previous.is_alive():
                self._housekeeping_stop.set()
                previous.join(timeout=5)
                if previous.is_alive():
                    raise AdapterError("RESPONSE_CACHE_HOUSEKEEPING_STOP_TIMEOUT")
            self._housekeeping_stop = threading.Event()
            stop = self._housekeeping_stop

            def run():
                while not stop.wait(interval_seconds):
                    try:
                        self.purge_expired_responses()
                    except sqlite3.Error:
                        # Do not change provider request/accounting state on
                        # transient database contention; retry next interval.
                        pass

            thread = threading.Thread(target=run, name="traveltech-response-retention", daemon=True)
            self._housekeeping_thread = thread
            thread.start()

    def close(self):
        with self._housekeeping_lock:
            self._housekeeping_stop.set()
            thread = self._housekeeping_thread
            if thread and thread.is_alive():
                thread.join(timeout=5)
                if thread.is_alive():
                    raise AdapterError("RESPONSE_CACHE_HOUSEKEEPING_STOP_TIMEOUT")
            with self.db_lock:
                self.db.close()

    def reserve(self, endpoint, payload, *, credential=None):
        with self.db_lock:
            return self._reserve_locked(endpoint, payload, credential=credential)

    def _reserve_locked(self, endpoint, payload, *, credential=None):
        grant = self.grant
        if (
            endpoint not in ENDPOINTS
            or grant.get("mode") != "LIVE_TEST"
            or grant.get("synthetic_only") is not True
            or grant.get("owner_authorized") is not True
        ):
            raise AdapterError("LIVE_TEST_DISABLED")
        cap = Decimal(str(grant.get("total_limit_usd", "0")))
        if not Decimal(0) < cap <= Decimal(".10"):
            raise AdapterError("LIVE_BUDGET_INVALID")
        provider, maximum, _ = ENDPOINTS[endpoint]
        model = payload.get("model")
        proofs = grant.get("free_credit_proofs")
        if isinstance(proofs, dict):
            if provider + "/" + str(model) not in proofs:
                raise AdapterError("FREE_ELIGIBILITY_ROUTE_UNVERIFIED")
            proof = proofs[provider + "/" + str(model)]
        else:
            proof = grant.get("free_credit_proof", {})
        age = time.time() - proof.get("observed_unix", 0)
        if proof.get("covered_by_free_credit") is not True or not 0 <= age <= 300:
            raise AdapterError("FREE_CREDIT_NOT_CURRENTLY_PROVEN")
        if proof.get("model") and proof.get("model") != model:
            raise AdapterError("FREE_ELIGIBILITY_MODEL_MISMATCH")
        if proof.get("provider") and proof.get("provider") != provider:
            raise AdapterError("FREE_ELIGIBILITY_PROVIDER_MISMATCH")
        pricing = grant.get("pricing_per_token", {}).get(provider + "/" + str(model))
        if not isinstance(pricing, dict) or any(
            Decimal(str(pricing.get(k, "-1"))) < 0 for k in ("input", "output")
        ):
            raise AdapterError("PRICE_UNKNOWN")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # A local credential/payload mistake must not create a phantom dispatched call.
        if credential is not None and credential.encode("utf-8") in encoded:
            raise AdapterError("SECRET_IN_PAYLOAD")
        if len(encoded) > 16384:
            raise AdapterError("LIVE_INPUT_TOO_LARGE")
        max_output = payload.get("max_tokens", 0)
        if provider != "Jev" and (type(max_output) is not int or not 1 <= max_output <= 3072):
            raise AdapterError("OUTPUT_BOUND_REQUIRED")
        digest = hashlib.sha256(encoded).hexdigest()
        # Per-byte input estimate is conservative relative to UTF-8 token counts.
        reserved = (
            Decimal(pricing["input"]) * len(encoded) + Decimal(pricing["output"]) * max_output
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            host = urlsplit(endpoint).netloc
            cooldown = self.db.execute(
                "SELECT until_unix FROM provider_backoff WHERE host=?", (host,)
            ).fetchone()
            if cooldown and cooldown[0] > time.time():
                raise AdapterError("PROVIDER_RATE_LIMIT_COOLDOWN")
            rows = self.db.execute(
                "SELECT provider,model,reserved,status,digest FROM calls"
            ).fetchall()
            maximum_total = grant.get("max_total_requests", 5)
            if type(maximum_total) is not int or not 1 <= maximum_total <= 5:
                raise AdapterError("TOTAL_REQUEST_LIMIT_INVALID")
            if len(rows) >= maximum_total:
                raise AdapterError("TOTAL_REQUEST_LIMIT_REACHED")
            # After receiving the provider response, a crash may still happen
            # before the caller receives it. Preserve the spent request instead
            # of silently re-sending identical chargeable inference.
            if any(
                r[0] == provider
                and r[1] == model
                and r[4] == digest
                and r[3] in ("RESERVED", "UNKNOWN_OUTCOME", "RESPONSE_RECEIVED")
                for r in rows
            ):
                raise AdapterError("PREVIOUS_IDENTICAL_OUTCOME_UNKNOWN")
            if sum(r[0] == provider for r in rows) >= maximum:
                raise AdapterError("PROVIDER_CALL_LIMIT")
            total = sum((Decimal(r[2]) for r in rows), Decimal(0)) + reserved
            if total > cap or total > Decimal(str(proof.get("free_credit_usd", "0"))):
                raise AdapterError("FREE_BUDGET_EXHAUSTED")
            identifier = secrets.token_hex(16)
            self.db.execute(
                "INSERT INTO calls VALUES(?,?,?,?,?,?)",
                (identifier, provider, model, str(reserved), "RESERVED", digest),
            )
            self.db.execute("COMMIT")
            return identifier, encoded
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def attempts(self):
        with self.db_lock:
            return [
                dict(zip(("id", "provider", "model", "reserved_usd", "status", "digest"), row))
                for row in self.db.execute(
                    "SELECT id,provider,model,reserved,status,digest FROM calls ORDER BY rowid"
                )
            ]

    def mark_result(self, identifier, status):
        with self.db_lock:
            self.db.execute("UPDATE calls SET status=? WHERE id=?", (status, identifier))

    @property
    def last_was_recovered(self):
        return getattr(self._recovery, "replayed", False)

    @property
    def last_response_handle(self):
        return getattr(self._recovery, "identifier", None)

    def purge_expired_responses(self):
        # Remove only encrypted bodies. Keep calls forever for immutable request
        # limits and RC12 duplicate prevention. No plaintext is written.
        with self.db_lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                deleted = self.db.execute(
                    "DELETE FROM model_responses WHERE saved_unix < ?", (time.time() - 300,)
                ).rowcount
                self.db.execute("COMMIT")
                return deleted
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def response_delivery_state(self, identifier):
        with self.db_lock:
            if self.db.execute(
                "SELECT 1 FROM model_processing_receipts WHERE call_id=?", (identifier,)
            ).fetchone():
                return "APPLICATION_PROCESSED"
            row = self.db.execute(
                "SELECT app_acknowledged_unix FROM model_responses WHERE call_id=?", (identifier,)
            ).fetchone()
            if row is not None:
                return "APPLICATION_PROCESSED" if row[0] is not None else "STORED_NOT_ACKNOWLEDGED"
            call = self.db.execute("SELECT status FROM calls WHERE id=?", (identifier,)).fetchone()
            if call and call[0] == "RESPONSE_RECEIVED":
                return "RESPONSE_UNAVAILABLE_UNACKNOWLEDGED"
            return "NO_RETAINED_RESPONSE"

    def acknowledge_application_result(self, identifier):
        # Called by the application AFTER independently validating and saving
        # its result. Never interpret as end-user/UI delivery acknowledgment.
        if not isinstance(identifier, str) or len(identifier) != 32:
            return False
        with self.db_lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    "SELECT r.call_id FROM model_responses r JOIN calls c "
                    "ON r.call_id=c.id WHERE r.call_id=? AND c.status='RESPONSE_RECEIVED' "
                    "AND r.saved_unix>=?",
                    (identifier, time.time() - 300),
                ).fetchone()
                if row is None:
                    self.db.execute("COMMIT")
                    return False
                processed_at = time.time()
                self.db.execute(
                    "INSERT OR IGNORE INTO model_processing_receipts(call_id,processed_unix) "
                    "VALUES(?,?)",
                    (identifier, processed_at),
                )
                self.db.execute(
                    "UPDATE model_responses SET app_acknowledged_unix=COALESCE("
                    "app_acknowledged_unix,?) WHERE call_id=?",
                    (processed_at, identifier),
                )
                self.db.execute("COMMIT")
                return True
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def store_response(self, identifier, endpoint, response):
        from response_protection import seal_reply

        data = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf8")
        if len(data) > 1048576:
            raise AdapterError("RESPONSE_CACHE_SIZE_EXCEEDED")
        with self.db_lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                call = self.db.execute(
                    "SELECT provider,model,digest,status FROM calls WHERE id=?", (identifier,)
                ).fetchone()
                if not call or call[3] != "RESERVED" or ENDPOINTS[endpoint][0] != call[0]:
                    raise AdapterError("RESPONSE_CACHE_RECEIPT_INVALID")
                sealed = seal_reply(data, identifier, call[0], call[1], call[2])
                self.db.execute(
                    "INSERT INTO model_responses(call_id,sealed_response,response_sha256,saved_unix) "
                    "VALUES(?,?,?,?)",
                    (identifier, sealed, hashlib.sha256(data).hexdigest(), time.time()),
                )
                self.db.execute(
                    "UPDATE calls SET status='RESPONSE_RECEIVED' WHERE id=?", (identifier,)
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        # Remove expired encrypted bodies while the process is idle; the
        # processing receipt is stored independently and must never be purged.
        with self._housekeeping_lock:
            if self._housekeeping_thread is None or not self._housekeeping_thread.is_alive():
                self.start_housekeeping()

    def recover_response(self, endpoint, payload):
        if (
            endpoint not in ENDPOINTS
            or self.grant.get("mode") != "LIVE_TEST"
            or self.grant.get("owner_authorized") is not True
            or self.grant.get("synthetic_only") is not True
        ):
            raise AdapterError("LIVE_TEST_DISABLED")
        from response_protection import open_reply

        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf8")
        if len(encoded) > 16384:
            raise AdapterError("LIVE_INPUT_TOO_LARGE")
        digest = hashlib.sha256(encoded).hexdigest()
        provider = ENDPOINTS[endpoint][0]
        model = payload.get("model")
        with self.db_lock:
            rows = self.db.execute(
                "SELECT c.id,c.status,r.sealed_response,r.response_sha256,r.saved_unix "
                "FROM calls c LEFT JOIN model_responses r ON r.call_id=c.id "
                "WHERE c.provider=? AND c.model=? AND c.digest=? ORDER BY c.rowid DESC",
                (provider, model, digest),
            ).fetchall()
        if not rows:
            return None
        if any(row[1] in ("RESERVED", "UNKNOWN_OUTCOME") for row in rows):
            return None
        found = next(
            (row for row in rows if row[1] == "RESPONSE_RECEIVED" and row[2] is not None), None
        )
        if found is None:
            return None
        if not 0 <= time.time() - found[4] <= 300:
            # Scrub expired ciphertext on next attempted recovery.
            self.purge_expired_responses()
            raise AdapterError("RESPONSE_CACHE_EXPIRED")
        data = open_reply(found[2], found[0], provider, model, digest)
        if hashlib.sha256(data).hexdigest() != found[3]:
            raise AdapterError("RESPONSE_CACHE_INTEGRITY_FAILED")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise AdapterError("RESPONSE_CACHE_INTEGRITY_FAILED") from None
        self._recovery.identifier = found[0]
        return value

    def mark_http_error(self, identifier, endpoint, code, retry_after=None):
        # Keep the attempt receipt and host-wide 429 backoff atomic.
        # A crash after one SQLite commit cannot lose just the cooldown.
        with self.db_lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                status = "UNKNOWN_OUTCOME" if code >= 500 else "RESPONSE_REJECTED"
                self.db.execute("UPDATE calls SET status=? WHERE id=?", (status, identifier))
                if code == 429:
                    from ai_router import retry_seconds

                    now = time.time()
                    delay = max(60.0, retry_seconds(retry_after, now))
                    self.db.execute(
                        "INSERT INTO provider_backoff(host,until_unix,status,recorded_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(host) DO UPDATE SET "
                        "until_unix=max(provider_backoff.until_unix,excluded.until_unix),"
                        "status=excluded.status,recorded_at=excluded.recorded_at",
                        (urlsplit(endpoint).netloc, now + delay, code, now),
                    )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def __call__(self, endpoint, payload):
        self._recovery.replayed = False
        self._recovery.identifier = None
        if endpoint not in ENDPOINTS:
            raise AdapterError("LIVE_TEST_DISABLED")
        _, _, filename = ENDPOINTS[endpoint]
        # Verify local prerequisites before reserving a charge or dispatching a request.
        # Missing/invalid credentials are never an UNKNOWN_OUTCOME.
        try:
            key = read_key(Path.home() / ".traveltech-secrets" / filename)
        except (OSError, ValueError, UnicodeError):
            raise AdapterError("GATEWAY_CREDENTIAL_PRECHECK_FAILED") from None
        cached = self.recover_response(endpoint, payload)
        if cached is not None:
            self._recovery.replayed = True
            return cached
        identifier, body = self.reserve(endpoint, payload, credential=key)
        try:
            req = urllib.request.Request(
                endpoint,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": "Bearer " + key,
                },
                method="POST",
            )
            from trusted_tls import verified_context

            opener = self.opener or urllib.request.build_opener(
                NoRedirect(), urllib.request.HTTPSHandler(context=verified_context())
            )
            with opener.open(req, timeout=self.timeout_seconds) as response:
                raw = response.read(1048577)
                if len(raw) > 1048576:
                    raise AdapterError("RESPONSE_TOO_LARGE_UNKNOWN_OUTCOME")
                if key.encode() in raw:
                    raise AdapterError("CREDENTIAL_ECHO_BLOCKED")
                parsed = json.loads(raw)
            self.store_response(identifier, endpoint, parsed)
            self._recovery.identifier = identifier
            return parsed
        except urllib.error.HTTPError as exc:
            code = exc.code
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            self.mark_http_error(identifier, endpoint, code, retry_after)
            error = AdapterError("HTTP_" + str(code))
            error.http_status = code
            error.retry_after = retry_after
            raise error from None
        except Exception as exc:
            # Never overwrite an already committed successful reply receipt.
            with self.db_lock:
                self.db.execute(
                    "UPDATE calls SET status='UNKNOWN_OUTCOME' WHERE id=? AND status='RESERVED'",
                    (identifier,),
                )
            raise AdapterError(
                str(exc) if isinstance(exc, AdapterError) else "TRANSPORT_UNKNOWN_OUTCOME"
            ) from None
