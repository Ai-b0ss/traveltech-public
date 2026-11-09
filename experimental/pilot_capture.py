"""Server-only Pilot search capture. A receipt authenticates origin, never price.

No HTTP capture/grant/import endpoint. Consent is supplied by trusted application
code after operator review, never deserialized from a client or supplier object.
The existing PilotClient owns transport, parsing and durable outbound protection.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import time

from provider_transport import canonical
from supplier_pilot import PilotClient, PilotError, validate_query
from trip_plan_session import extract_session_context

TTL_SECONDS = 300  # local display-cache TTL, NOT provider quote expiry
NOTICE = ("Pilot: search observation only. Full-stay price, taxes, fees, occupancy "
          "and provider freshness are unverified; excluded from trip total.")


@dataclass(frozen=True)
class CaptureConsent:
    """Trusted operator record, scoped to one stored request. Not an HTTP DTO.

    References are non-personal internal review IDs, not secrets or legal proof.
    commercial_display requires an independently reviewed rights reference.
    """

    owner: str
    sid: str
    revision: int
    request_sha256: str
    guest_nationality: str
    consent_reference: str
    audience: str = "internal_research"
    rights_reference: str | None = None


class PilotCapture:
    def __init__(self, app):
        self.app = app
        app.store.db.execute("""CREATE TABLE IF NOT EXISTS pilot_observation_captures(
            owner TEXT NOT NULL, sid TEXT NOT NULL, revision INTEGER NOT NULL,
            state TEXT NOT NULL, body TEXT NOT NULL, signature TEXT NOT NULL,
            PRIMARY KEY(owner,sid,revision))""")

    @staticmethod
    def enabled():
        return (os.environ.get("TRAVELTECH_PILOT_CAPTURE_ENABLED") == "1"
                and os.environ.get("TRAVELTECH_PILOT_ENABLED") == "1")

    def _context(self, owner, sid, revision):
        if (not isinstance(owner, str) or len(owner) < 8
                or not isinstance(sid, str) or not re.fullmatch("[a-f0-9]{32}", sid)
                or type(revision) is not int or not 0 <= revision <= 2**53 - 1):
            raise PilotError("PILOT_CAPTURE_SCOPE_INVALID")
        db = self.app.store.db
        if db.execute("SELECT owner FROM session_owners WHERE id=?", (sid,)).fetchone() != (owner,):
            raise PilotError("CAPABILITY_NOT_OWNED")
        saved = self.app.store.get_session(sid)
        if not isinstance(saved, dict) or saved.get("schema") != 1:
            raise PilotError("PILOT_CAPTURE_SESSION_INVALID")
        state = saved.get("state")
        if (not isinstance(state, dict) or type(state.get("revision")) is not int
                or not isinstance(state.get("request"), dict)):
            raise PilotError("PILOT_CAPTURE_SESSION_INVALID")
        if state["revision"] != revision:
            raise PilotError("STALE_REVISION")
        digest = hashlib.sha256(canonical(state["request"]).encode()).hexdigest()
        return saved, digest

    def scope(self, owner, sid, revision):
        """Read-only preflight for trusted operator consent; no supplier call."""
        with self.app.lock, self.app.store.transaction():
            return self._context(owner, sid, revision)[1]

    def _write(self, owner, sid, revision, state, body):
        envelope = {"owner": owner, "sid": sid, "revision": revision,
                    "state": state, "receipt": body}
        self.app.store.db.execute(
            "INSERT OR REPLACE INTO pilot_observation_captures VALUES(?,?,?,?,?,?)",
            (owner, sid, revision, state, canonical(body), self.app.store.sign(envelope)))

    def capture(self, consent):
        """One search per owner/sid/revision, no rates, retries or imported data.

        Reservation commits before dispatch. Crash/error replay is a HOLD, even
        if transport had received a response. A new revision is a new consent.
        """
        if not self.enabled():
            raise PilotError("PILOT_CAPTURE_DISABLED")
        if type(consent) is not CaptureConsent:
            raise PilotError("PILOT_CAPTURE_SERVER_CONSENT_REQUIRED")
        if (consent.audience not in ("internal_research", "commercial_display")
                or not isinstance(consent.consent_reference, str)
                or not re.fullmatch("[A-Za-z0-9_-]{8,100}", consent.consent_reference)
                or (consent.rights_reference is not None and
                    (not isinstance(consent.rights_reference, str) or not re.fullmatch(
                        "[A-Za-z0-9_-]{8,100}", consent.rights_reference)))
                or (consent.audience == "commercial_display" and
                    consent.rights_reference is None)):
            raise PilotError("PILOT_CAPTURE_PERMISSION_REQUIRED")
        owner, sid, revision = consent.owner, consent.sid, consent.revision
        store = self.app.store
        with self.app.lock:
            if store.db.in_transaction:
                raise PilotError("PILOT_CAPTURE_DURABLE_BOUNDARY_REQUIRED")
            with store.transaction():
                saved, digest = self._context(owner, sid, revision)
                if digest != consent.request_sha256:
                    raise PilotError("PILOT_CAPTURE_CONSENT_CONTEXT_CHANGED")
                # Exact stored room allocation is preserved alongside the aggregate
                # query. Neither aggregate nor supplier results verify occupancy.
                context = extract_session_context(saved, session_id=sid)
                query = validate_query({
                    "location": context.destination, "check_in": context.start_date,
                    "check_out": context.end_date, "guests": context.adults,
                    "rooms": len(context.rooms), "children_ages": list(context.children),
                    "currency": context.currency, "guest_nationality": consent.guest_nationality,
                    "limit": 4,
                })
                if store.db.execute("SELECT 1 FROM pilot_observation_captures "
                        "WHERE owner=? AND sid=? AND revision=?", (owner, sid, revision)).fetchone():
                    raise PilotError("PILOT_CAPTURE_PRIOR_ATTEMPT_NO_RETRY")
                started = time.time()
                receipt = {
                    "request_sha256": digest, "query": query, "rooms": context.rooms_dict(),
                    "consent_reference": consent.consent_reference,
                    "audience": consent.audience, "rights_reference": consent.rights_reference,
                    "started_at": started, "cache_expires_at": started + TTL_SECONDS,
                    "origin": "SERVER_PILOT_SEARCH_V1", "provider_freshness": "UNKNOWN",
                }
                self._write(owner, sid, revision, "RESERVED", receipt)
        # No app/SQLite lock held across external IO. No caller-supplied client,
        # endpoint, result, ID, source_mode, verification flag or price accepted.
        request_id = "capture-" + hashlib.sha256(canonical([owner, sid, revision]).encode()).hexdigest()
        client = PilotClient(self.app.storage / "pilot_capture", owner=owner, request_id=request_id)
        try:
            result = client.search(query)  # exactly ONE bounded read-only call
            received = time.time()
            observed = datetime.fromisoformat(result["observed_at"])
            if (observed.tzinfo is None or not started <= observed.timestamp() <= received
                    or received >= receipt["cache_expires_at"]):
                raise PilotError("PILOT_CAPTURE_SOURCE_STALE")
            # Strip process-local handles. All remaining fields came through the
            # existing adapter's allowlist, never from an import/normalize API.
            observations = [{k: v for k, v in row.items() if k != "research_handle"}
                            for row in result["observations"][:query["limit"]]]
            receipt.update(observed_at=observed.astimezone(timezone.utc).isoformat(),
                           received_at=received, observations=observations)
            with self.app.lock, store.transaction():
                _, current_digest = self._context(owner, sid, revision)
                if current_digest != digest:
                    raise PilotError("PILOT_CAPTURE_CONTEXT_CHANGED")
                if store.db.execute("SELECT state FROM pilot_observation_captures "
                        "WHERE owner=? AND sid=? AND revision=?", (owner, sid, revision)).fetchone() != ("RESERVED",):
                    raise PilotError("PILOT_CAPTURE_WITHDRAWN")
                self._write(owner, sid, revision, "COMPLETE", receipt)
        except Exception as error:
            # No exception text/body/URL is logged, including unknown provider text.
            code = str(error) if isinstance(error, PilotError) else "PILOT_CAPTURE_FAILED_UNKNOWN_OUTCOME"
            if not re.fullmatch("[A-Z][A-Z0-9_]{0,100}", code):
                code = "PILOT_CAPTURE_FAILED_UNKNOWN_OUTCOME"
            receipt.pop("observations", None)
            receipt["error"] = code
            with self.app.lock, store.transaction():
                # Withdrawal during external IO wins; never restore its payload.
                if store.db.execute("SELECT state FROM pilot_observation_captures "
                        "WHERE owner=? AND sid=? AND revision=?", (owner, sid, revision)).fetchone() == ("RESERVED",):
                    self._write(owner, sid, revision, "HOLD", receipt)
            raise PilotError(code) from None
        return self.read(owner, sid, revision, internal=True)

    def withdraw(self, owner, sid, revision):
        """Server/operator revocation; retain a tombstone so it cannot retry IO."""
        with self.app.lock, self.app.store.transaction():
            _, digest = self._context(owner, sid, revision)
            self._write(owner, sid, revision, "WITHDRAWN", {
                "request_sha256": digest, "origin": "SERVER_PILOT_SEARCH_V1"})

    def read(self, owner, sid, revision, *, internal=False):
        """No network; signed, current, owner-bound observation display only.

        HTTP always uses internal=False. Internal research records are withheld
        from that surface until independently reviewed commercial rights exist.
        """
        def empty(status):
            return {"status": status, "observations": [], "total_amount": None,
                    "verification": "UNVERIFIED", "notice": NOTICE}

        if not self.enabled():
            return empty("DISABLED")
        with self.app.lock, self.app.store.transaction():
            _, digest = self._context(owner, sid, revision)
            row = self.app.store.db.execute(
                "SELECT state,body,signature FROM pilot_observation_captures "
                "WHERE owner=? AND sid=? AND revision=?", (owner, sid, revision)).fetchone()
            if not row:
                return empty("NOT_CAPTURED")
            try:
                state, raw, signature = row
                receipt = json.loads(raw)
                signed = {"owner": owner, "sid": sid, "revision": revision,
                          "state": state, "receipt": receipt}
                if not hmac.compare_digest(signature, self.app.store.sign(signed)):
                    return empty("INTEGRITY_HOLD")
                if (receipt["request_sha256"] != digest
                        or receipt["origin"] != "SERVER_PILOT_SEARCH_V1"):
                    return empty("CONTEXT_HOLD")
                if state != "COMPLETE":
                    return empty("UNKNOWN_OUTCOME_NO_RETRY" if state == "RESERVED"
                                 else "WITHDRAWN" if state == "WITHDRAWN" else "HOLD")
                clock = time.time()
                if not receipt["started_at"] <= clock < receipt["cache_expires_at"]:
                    return empty("STALE")
                if not internal and (receipt["audience"] != "commercial_display"
                                     or not receipt["rights_reference"]):
                    return empty("COMMERCIAL_RIGHTS_HOLD")
                # A persisted response is CACHED even inside its local cache TTL.
                observations = receipt["observations"]
                for observation in observations:
                    observation["source_mode"] = "CACHED"
                    if "NOT_LIVE" not in observation["warnings"]:
                        observation["warnings"].append("NOT_LIVE")
                return {**empty("OBSERVATIONS_AVAILABLE" if observations else "NO_OFFERS"),
                        "observations": observations, "session_id": sid, "revision": revision,
                        "request_sha256": digest, "query": receipt["query"], "rooms": receipt["rooms"],
                        "observed_at": receipt["observed_at"], "received_at": receipt["received_at"],
                        "cache_expires_at": receipt["cache_expires_at"],
                        "provider_freshness": "UNKNOWN", "price_expires_at": None,
                        "capture_origin": "SERVER_PILOT_SEARCH_V1"}
            except (KeyError, TypeError, ValueError):
                return empty("INTEGRITY_HOLD")
