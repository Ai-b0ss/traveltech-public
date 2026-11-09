"""Durable, owner-scoped read-only hotel search snapshots.

Saved supplier observations are not bookings or guaranteed payable quotes.
"""

import hashlib
import json
import secrets
import time
from provider_transport import canonical, Failure


class LiveSnapshotStore:
    MAX_BYTES = 8_000_000
    MAX_AGE_SECONDS = 300

    def __init__(self, app):
        self.app = app
        with self.app.lock:
            self.app.store.db.execute(
                """CREATE TABLE IF NOT EXISTS live_snapshots(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    sid TEXT NOT NULL, revision INTEGER NOT NULL,
                    request_hash TEXT NOT NULL, saved_at REAL NOT NULL,
                    payload TEXT NOT NULL)"""
            )
            self.app.store.db.execute(
                "CREATE INDEX IF NOT EXISTS live_snapshots_owner_trip "
                "ON live_snapshots(owner,sid,revision,saved_at)"
            )
            self.app.store.db.execute(
                """CREATE TABLE IF NOT EXISTS selected_supplier_offers(
                    owner TEXT NOT NULL, sid TEXT NOT NULL, revision INTEGER NOT NULL,
                    snapshot_id TEXT NOT NULL, offer_index INTEGER NOT NULL,
                    hotel_id TEXT NOT NULL, room_id TEXT NOT NULL, rate_code TEXT NOT NULL,
                    saved_at REAL NOT NULL, PRIMARY KEY(owner,sid,revision))"""
            )
            self.app.store.db.execute(
                """CREATE TABLE IF NOT EXISTS supplier_rechecks(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, sid TEXT NOT NULL,
                    revision INTEGER NOT NULL, snapshot_id TEXT NOT NULL,
                    offer_index INTEGER NOT NULL, observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL, accepted_snapshot_id TEXT)"""
            )

        self._migrate_existing_sources()

    def _migrate_existing_sources(self):
        # RC18 already persisted owner-scoped observations separately. Reuse
        # them on upgrade without contacting a supplier or minting freshness.
        with self.app.lock, self.app.store.transaction():
            if not self.app.store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_owners'"
            ).fetchone():
                return
            rows = self.app.store.db.execute(
                "SELECT l.owner,l.sid,l.revision,l.id FROM live_snapshots l JOIN session_owners o ON o.id=l.sid AND o.owner=l.owner ORDER BY l.saved_at DESC,l.rowid DESC"
            ).fetchall()
            for owner, sid, revision, identifier in rows:
                body = self.app.store.get_session(sid)
                if (
                    not body
                    or "source_mode" in body
                    or body.get("state", {}).get("revision") != revision
                ):
                    continue
                try:
                    snapshot = self.load(owner, sid, revision, identifier)
                except Failure:
                    continue
                snapshot["request"] = body["state"]["request"]
                body.update(
                    source_mode="live",
                    source_ref=self.app._store_source(snapshot),
                    source_snapshot_id=identifier,
                )
                selected = self.selection(owner, sid, revision)["selected_offer"]
                if selected is not None:
                    body["selected_supplier_offer"] = {"revision": revision, "value": selected}
                    body["state"]["selected_offer"] = [
                        selected[k] for k in ("hotel_id", "room_id", "rate_code")
                    ]
                self.app.store.put_session(sid, body)

    def _current_request(self, sid, revision):
        self.app._path(sid)
        body = self.app.store.get_session(sid)
        if body is None:
            raise Failure("SESSION_NOT_FOUND")
        state = body.get("state") or {}
        if type(revision) is not int or state.get("revision") != revision:
            raise Failure("STALE_REVISION")
        return state["request"]

    def save(self, owner, sid, revision, request, result):
        if not isinstance(result, dict) or not isinstance(result.get("offers"), list):
            raise Failure("LIVE_SNAPSHOT_INVALID")
        with self.app.lock, self.app.store.transaction():
            current = self._current_request(sid, revision)
            fingerprint = hashlib.sha256(canonical(current).encode("utf8")).hexdigest()
            if canonical(request) != canonical(current):
                raise Failure("STALE_SEARCH_CONTEXT")
            snapshot_id = secrets.token_hex(16)
            saved_at = time.time()
            payload = dict(
                result,
                session_id=sid,
                revision=revision,
                source_snapshot_id=snapshot_id,
                snapshot_saved_at=saved_at,
                availability_verified=False,
            )
            encoded = canonical(payload)
            if len(encoded.encode("utf8")) > self.MAX_BYTES:
                raise Failure("LIVE_SNAPSHOT_TOO_LARGE")
            self.app.store.db.execute(
                "INSERT INTO live_snapshots VALUES(?,?,?,?,?,?,?)",
                (snapshot_id, owner, sid, revision, fingerprint, saved_at, encoded),
            )
            # The observation is the trip's source, not an unrelated UI cache.
            body = self.app.store.get_session(sid)
            body.update(
                source_mode="live",
                source_ref=self.app._store_source(dict(payload, request=current)),
                source_snapshot_id=snapshot_id,
            )
            body.pop("source_result", None)
            body.pop("selected_supplier_offer", None)
            body["state"]["selected_offer"] = None
            self.app.store.put_session(sid, body)
            return payload

    def load(self, owner, sid, revision, snapshot_id=None):
        with self.app.lock:
            current = self._current_request(sid, revision)
            current_hash = hashlib.sha256(canonical(current).encode("utf8")).hexdigest()
            if snapshot_id is not None:
                if (
                    not isinstance(snapshot_id, str)
                    or len(snapshot_id) != 32
                    or any(c not in "0123456789abcdef" for c in snapshot_id)
                ):
                    raise Failure("SNAPSHOT_ID_INVALID")
                row = self.app.store.db.execute(
                    "SELECT request_hash,saved_at,payload FROM live_snapshots "
                    "WHERE id=? AND owner=? AND sid=? AND revision=?",
                    (snapshot_id, owner, sid, revision),
                ).fetchone()
            else:
                row = self.app.store.db.execute(
                    "SELECT request_hash,saved_at,payload FROM live_snapshots "
                    "WHERE owner=? AND sid=? AND revision=? ORDER BY saved_at DESC,rowid DESC LIMIT 1",
                    (owner, sid, revision),
                ).fetchone()
            if not row:
                raise Failure("LIVE_SNAPSHOT_NOT_FOUND_OR_NOT_OWNED")
            if row[0] != current_hash:
                raise Failure("LIVE_SNAPSHOT_CONTEXT_MISMATCH")
            age = max(0, time.time() - row[1])
            result = json.loads(row[2])
            from datetime import datetime, timezone

            try:
                stamp = datetime.fromisoformat(result["observed_at"])
                observed_age = (datetime.now(timezone.utc) - stamp).total_seconds()
                age = max(age, observed_age) if observed_age >= 0 else self.MAX_AGE_SECONDS + 1
            except (KeyError, TypeError, ValueError):
                pass
            return dict(
                result,
                snapshot_age_seconds=round(age, 2),
                snapshot_stale=age > self.MAX_AGE_SECONDS or result.get("snapshot_stale") is True,
                snapshot_is_booking=False,
                snapshot_warning="Historic supplier observation; not a confirmed booking or final payable price",
            )

    def choose(self, owner, sid, revision, snapshot_id, offer_index):
        # Selecting is an advisory bookmark; no price or inventory guarantee.
        if type(offer_index) is not int:
            raise Failure("OFFER_INDEX_INVALID")
        with self.app.lock, self.app.store.transaction():
            snapshot = self.load(owner, sid, revision, snapshot_id)
            from pet_preferences import active

            pet = active(self.app.store.get_session(sid).get("preferences", []))
            if pet and pet["travelling_with_pet"]:
                raise Failure("PET_POLICY_UNVERIFIED")
            if snapshot["snapshot_stale"]:
                raise Failure("STALE_SOURCE_SNAPSHOT_RESEARCH_AGAIN")
            offers = snapshot["offers"]
            if not 0 <= offer_index < len(offers):
                raise Failure("OFFER_INDEX_INVALID")
            offer = offers[offer_index]
            if offer.get("recommendable") is False:
                raise Failure("HARD_REQUIREMENTS_NOT_CONFIRMED")
            pending = self.app.store.db.execute(
                "SELECT payload FROM supplier_rechecks WHERE owner=? AND sid=? AND revision=? AND snapshot_id=? AND offer_index=? ORDER BY rowid DESC LIMIT 1",
                (owner, sid, revision, snapshot_id, offer_index),
            ).fetchone()
            if pending and json.loads(pending[0]).get("requires_user_reacceptance"):
                raise Failure("QUOTE_CHANGED_EXPLICIT_ACCEPTANCE_REQUIRED")
            if offer.get("status") == "REJECT":
                raise Failure("REJECTED_RATE_CANNOT_BE_SELECTED")
            identity = []
            for field in ("hotel_id", "room_id", "rate_code"):
                value = offer.get(field)
                if not isinstance(value, str) or not 1 <= len(value) <= 256:
                    raise Failure("UNVERIFIED_OFFER_IDENTITY")
                identity.append(value)
            self.app.store.db.execute(
                """INSERT INTO selected_supplier_offers
                (owner,sid,revision,snapshot_id,offer_index,hotel_id,room_id,rate_code,saved_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(owner,sid,revision) DO UPDATE SET
                snapshot_id=excluded.snapshot_id,offer_index=excluded.offer_index,
                hotel_id=excluded.hotel_id,room_id=excluded.room_id,
                rate_code=excluded.rate_code,saved_at=excluded.saved_at""",
                (owner, sid, revision, snapshot_id, offer_index, *identity, time.time()),
            )
            result = self.selection(owner, sid, revision)
            body = self.app.store.get_session(sid)
            body["selected_supplier_offer"] = {
                "revision": revision,
                "value": result["selected_offer"],
            }
            body["state"]["selected_offer"] = identity
            self.app.store.put_session(sid, body)
            return result

    def selection(self, owner, sid, revision):
        with self.app.lock:
            self._current_request(sid, revision)
            row = self.app.store.db.execute(
                """SELECT snapshot_id,offer_index,hotel_id,room_id,rate_code,saved_at
                FROM selected_supplier_offers WHERE owner=? AND sid=? AND revision=?""",
                (owner, sid, revision),
            ).fetchone()
            if row is None:
                return {"selected_offer": None, "booking_performed": False}
            snapshot = self.load(owner, sid, revision, row[0])
            offers = snapshot["offers"]
            if not 0 <= row[1] < len(offers):
                raise Failure("SELECTED_OFFER_CORRUPT")
            offer = offers[row[1]]
            if tuple(offer.get(k) for k in ("hotel_id", "room_id", "rate_code")) != tuple(row[2:5]):
                raise Failure("SELECTED_OFFER_IDENTITY_MISMATCH")
            return {
                "selected_offer": {
                    "source_snapshot_id": row[0],
                    "offer_index": row[1],
                    "hotel_id": row[2],
                    "room_id": row[3],
                    "rate_code": row[4],
                    "hotel_name": offer.get("hotel_name"),
                    "room_name": offer.get("room_name"),
                    "price_observed": offer.get("total"),
                    "currency_observed": offer.get("currency"),
                    "saved_at": row[5],
                    "snapshot_stale": snapshot["snapshot_stale"],
                    "availability_verified": False,
                    "payable_price_confirmed": False,
                    "selection_kind": "USER_BOOKMARK_NOT_BOOKING",
                },
                "booking_performed": False,
            }

    def record_check(self, owner, sid, revision, snapshot_id, offer_index, result):
        """Persist recheck separately; never silently overwrite the agreed price."""
        from datetime import datetime, timezone

        with self.app.lock, self.app.store.transaction():
            snapshot = self.load(owner, sid, revision, snapshot_id)
            if type(offer_index) is not int or not 0 <= offer_index < len(snapshot["offers"]):
                raise Failure("OFFER_INDEX_INVALID")
            offer = snapshot["offers"][offer_index]
            for field, expected in [
                ("hotel_id", offer["hotel_id"]),
                ("room_id", offer["room_id"]),
                ("requested_rate_code", offer["rate_code"]),
            ]:
                if result.get(field) is not None and str(result[field]) != str(expected):
                    raise Failure("RECHECK_IDENTITY_MISMATCH")
            stamp = result.get("observed_at")
            valid_stamp = True
            try:
                dt = datetime.fromisoformat(stamp)
                if (
                    dt.tzinfo is None
                    or not 0 <= (datetime.now(timezone.utc) - dt).total_seconds() <= 300
                ):
                    raise ValueError()
            except (TypeError, ValueError):
                stamp = datetime.now(timezone.utc).isoformat()
                valid_stamp = False
            identifier = secrets.token_hex(16)
            saved = dict(
                result,
                availability_verified=bool(valid_stamp and result.get("availability_verified")),
                check_id=identifier,
                session_id=sid,
                revision=revision,
                source_snapshot_id=snapshot_id,
                offer_index=offer_index,
                previous_quote={
                    "total": offer.get("total"),
                    "currency": offer.get("currency"),
                    "cancellation": offer.get("cancellation"),
                },
                booking_performed=False,
            )
            candidates = [
                r
                for r in saved.get("checked_rates", [])
                if r.get("rate_binding") is True
                and r.get("identity_verified") is True
                and r.get("quote_recheck_verified") is True
                and r.get("rate_code") == offer["rate_code"]
            ]
            saved["can_accept_changed_quote"] = bool(
                saved.get("requires_user_reacceptance")
                and len(candidates) == 1
                and result.get("observed_at") == stamp
            )
            encoded = canonical(saved)
            if len(encoded.encode()) > self.MAX_BYTES:
                raise Failure("RECHECK_TOO_LARGE")
            self.app.store.db.execute(
                "INSERT INTO supplier_rechecks VALUES(?,?,?,?,?,?,?,?,NULL)",
                (identifier, owner, sid, revision, snapshot_id, offer_index, stamp, encoded),
            )
            body = self.app.store.get_session(sid)
            current = self.app._source_result(body)
            source = dict(
                current
                if current and current.get("source_snapshot_id") == snapshot_id
                else snapshot
            )
            source["offers"] = json.loads(canonical(source["offers"]))
            source["offers"][offer_index]["availability_check"] = {
                k: saved.get(k)
                for k in (
                    "check_id",
                    "observed_at",
                    "availability_verified",
                    "requires_user_reacceptance",
                    "status",
                )
            }
            if saved.get("requires_user_reacceptance"):
                source["offers"][offer_index]["quote_change"] = {
                    "check_id": identifier,
                    "requires_user_reacceptance": True,
                    "previous": saved["previous_quote"],
                    "candidates": candidates,
                }
                self.app.store.db.execute(
                    "DELETE FROM selected_supplier_offers WHERE owner=? AND sid=? AND revision=?",
                    (owner, sid, revision),
                )
            body["source_ref"] = self.app._store_source(source)
            if saved.get("requires_user_reacceptance"):
                body.pop("selected_supplier_offer", None)
                body["state"]["selected_offer"] = None
            self.app.store.put_session(sid, body)
            return saved

    def accept_quote(self, owner, sid, revision, snapshot_id, offer_index, check_id):
        from datetime import datetime, timezone
        from live_data import verify_offers

        with self.app.lock, self.app.store.transaction():
            row = self.app.store.db.execute(
                "SELECT observed_at,payload,accepted_snapshot_id FROM supplier_rechecks WHERE id=? AND owner=? AND sid=? AND revision=? AND snapshot_id=? AND offer_index=?",
                (check_id, owner, sid, revision, snapshot_id, offer_index),
            ).fetchone()
            if not row:
                raise Failure("RECHECK_NOT_FOUND_OR_NOT_OWNED")
            if row[2]:
                return {
                    "snapshot": self.load(owner, sid, revision, row[2]),
                    "accepted_quote": check_id,
                    "booking_performed": False,
                }
            snapshot = self.load(owner, sid, revision, snapshot_id)
            body = self.app.store.get_session(sid)
            if snapshot["snapshot_stale"] or body.get("source_snapshot_id") != snapshot_id:
                raise Failure("STALE_SOURCE_SNAPSHOT_RESEARCH_AGAIN")
            latest = self.app.store.db.execute(
                "SELECT id FROM supplier_rechecks WHERE owner=? AND sid=? AND revision=? AND snapshot_id=? AND offer_index=? ORDER BY rowid DESC LIMIT 1",
                (owner, sid, revision, snapshot_id, offer_index),
            ).fetchone()
            if latest != (check_id,):
                raise Failure("RECHECK_SUPERSEDED")
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds()
            if not 0 <= age <= 300:
                raise Failure("RECHECK_EXPIRED")
            check = json.loads(row[1])
            if check.get("can_accept_changed_quote") is not True:
                raise Failure("CHANGED_QUOTE_UNVERIFIED")
            offer = snapshot["offers"][offer_index]
            choices = [
                r
                for r in check["checked_rates"]
                if r.get("quote_recheck_verified") is True
                and r.get("rate_code") == offer["rate_code"]
            ]
            if len(choices) != 1:
                raise Failure("CHANGED_QUOTE_AMBIGUOUS")
            choice = choices[0]
            policy = choice.get("cancellation") or {}
            offer.update(
                total=choice["total"],
                currency=choice["currency"],
                observed_at=row[0],
                cancellation={
                    "effective_non_refundable": policy.get("refundable") is False,
                    "cancelPolicyInfos": policy.get("cancelPolicyInfos"),
                },
                availability_verified=False,
                quote_change=None,
                availability_check={
                    "check_id": check_id,
                    "observed_at": row[0],
                    "availability_verified": False,
                    "quote_accepted": True,
                },
            )
            snapshot["offers"] = verify_offers(
                snapshot["offers"], snapshot["request"], snapshot.get("fx")
            )
            snapshot["accepted_quote"] = {
                "check_id": check_id,
                "accepted_at": datetime.now(timezone.utc).isoformat(),
                "total": choice["total"],
                "currency": choice["currency"],
                "acceptance_kind": "READ_ONLY_QUOTE_NOT_BOOKING",
            }
            from offer_state import project

            cards, normalized = project(snapshot["request"], snapshot)
            by_identity = {tuple(c["identity"][1:4]): c for c in cards}
            for o in normalized["offers"]:
                c = by_identity[tuple(o[k] for k in ("hotel_id", "room_id", "rate_code"))]
                o.update(status=c["status"], reasons=c["reasons"], recommendable=c["recommendable"])
            saved = self.save(owner, sid, revision, snapshot["request"], normalized)
            self.app.store.db.execute(
                "UPDATE supplier_rechecks SET accepted_snapshot_id=? WHERE id=?",
                (saved["source_snapshot_id"], check_id),
            )
            return {"snapshot": saved, "accepted_quote": check_id, "booking_performed": False}
