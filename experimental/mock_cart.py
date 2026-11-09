"""Independent DEMO ONLY cart/order backend (#52/#53). No external side effects.

Callers supply opaque owner IDs and synthetic TripPlan data, never PII. The
trusted caller must supply the saved trip's current revision on every operation.
This module does not read or update application/session/provider state.
"""

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

try:
    from .trip_plan import TripPlanError, assemble_trip_plan, parse_amount
except ImportError:  # Existing experimental modules also support direct imports.
    from trip_plan import TripPlanError, assemble_trip_plan, parse_amount


class CartError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _revision(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise CartError("REVISION")


def _identifier(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) is None:
        raise CartError("IDENTIFIER")


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys():
        raise CartError("PLAN_FIELDS")
    if value.keys() - set(required) - set(optional):
        raise CartError("PLAN_FIELDS")


def _money(value):
    if value is not None and (
        not isinstance(value, str) or re.fullmatch(r"[0-9]+\.[0-9]{2}", value) is None
    ):
        raise CartError("AMOUNT_TYPE")
    parse_amount(value)


def _plan(source, expected_revision):
    """Validate the v0 boundary, then use the canonical assembler for arithmetic."""
    _revision(expected_revision)
    _fields(
        source,
        (
            "trip_id",
            "revision",
            "travellers",
            "origin",
            "destination",
            "start_date",
            "end_date",
            "currency",
            "budget",
            "hotel",
            "transport",
            "transfer",
            "total_amount",
            "warnings",
        ),
        ("insurance", "session_id"),
    )
    _revision(source["revision"])
    if source["revision"] != expected_revision:
        raise CartError("STALE_REVISION")
    if "session_id" in source:
        _identifier(source["session_id"])
        if source["trip_id"] != f"session-{source['session_id']}-rev{source['revision']}":
            raise CartError("PLAN_FIELDS")
    for name in ("trip_id", "origin", "destination"):
        if not isinstance(source[name], str) or not source[name].strip():
            raise CartError("PLAN_FIELDS")
    currency = source["currency"]
    if not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise CartError("CURRENCY")
    _fields(source["travellers"], ("adults", "children"))
    adults, children = source["travellers"]["adults"], source["travellers"]["children"]
    if (
        type(adults) is not int
        or adults < 1
        or not isinstance(children, list)
        or any(type(age) is not int or not 0 <= age < 18 for age in children)
    ):
        raise CartError("TRAVELLERS")
    _fields(source["budget"], ("amount", "currency", "scope"))
    _money(source["budget"]["amount"])
    if source["budget"]["currency"] != currency:
        raise CartError("MIXED_CURRENCY")
    if source["budget"]["amount"] is not None and source["budget"]["scope"] == "unspecified":
        raise CartError("BUDGET_SCOPE")
    extras = {
        "hotel": (("checkin", "checkout"), ("name",)),
        "transport": (
            ("kind", "departure_at", "arrival_at"),
            ("return_departure_at", "return_arrival_at"),
        ),
        "transfer": (("fare_scope", "duration_minutes", "pickup_at"), ("kind",)),
        "insurance": ((), ()),
    }
    for name, (required, optional) in extras.items():
        if name == "insurance" and name not in source:
            continue
        item = source[name]
        _fields(
            item,
            ("amount", "currency", "source_mode", "provider") + required,
            optional + ("fare_status", "synthetic", "source_note", "verification_errors"),
        )
        if "synthetic" in item and (item["synthetic"] is not True
                or item["source_mode"] not in {"MOCK", "SYNTHETIC", "TEST", "SANDBOX"}):
            raise CartError("SOURCE")
        if "source_note" in item and (not isinstance(item["source_note"], str)
                                       or not item["source_note"].strip()):
            raise CartError("SOURCE")
        if "verification_errors" in item:
            errors = item["verification_errors"]
            if (not isinstance(errors, list) or not all(isinstance(e, str) and e for e in errors)
                    or (errors and item["amount"] is not None)):
                raise CartError("SOURCE")
        _money(item["amount"])
        if item["currency"] != currency:  # Also reject unknown prices in another currency.
            raise CartError("MIXED_CURRENCY")
        # Preserve labels verbatim; the order mode must never relabel its source.
        for field in ("source_mode", "provider"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise CartError("SOURCE")
        for field in ("name", "kind"):
            if field in item and (not isinstance(item[field], str) or not item[field].strip()):
                raise CartError("PLAN_FIELDS")
        if item.get("fare_status") not in (None, "UNKNOWN", "AVAILABLE"):
            raise CartError("STALE_CART")
    _money(source["total_amount"])
    if not isinstance(source["warnings"], list):
        raise CartError("PLAN_FIELDS")
    for warning in source["warnings"]:
        _fields(warning, ("code", "component", "message"))
        if any(not isinstance(v, str) or not v.strip() for v in warning.values()):
            raise CartError("PLAN_FIELDS")
        if warning["code"] == "STALE_REVISION":
            raise CartError("STALE_CART")
    plan = assemble_trip_plan(source, expected_revision=expected_revision)
    # Retain source caveats in addition to the assembler's derived warnings.
    for warning in source["warnings"]:
        if warning not in plan["warnings"]:
            plan["warnings"].append(dict(warning))
    return plan


@dataclass(frozen=True, init=False)
class CartSnapshot:
    """Immutable serialized deep copy; every exposed dict is a fresh copy."""

    owner: str
    cart_id: str
    revision: int
    _plan_json: str

    def __init__(self, source, *, owner, cart_id, expected_revision):
        _identifier(owner)
        _identifier(cart_id)
        try:
            # JSON round-trip rejects non-JSON values and detaches nested objects.
            plan = _plan(json.loads(_canonical(source)), expected_revision)
        except TripPlanError as error:
            raise CartError(error.code) from error
        except CartError:
            raise
        except (ValueError, TypeError, OverflowError, RecursionError) as error:
            raise CartError("PLAN_FIELDS") from error
        for name, value in (
            ("owner", owner),
            ("cart_id", cart_id),
            ("revision", expected_revision),
            ("_plan_json", _canonical(plan)),
        ):
            object.__setattr__(self, name, value)

    @property
    def trip_plan(self):
        return json.loads(self._plan_json)

    def to_dict(self):
        return {
            "owner": self.owner,
            "cart_id": self.cart_id,
            "revision": self.revision,
            "trip_plan": self.trip_plan,
        }

    @property
    def sha256(self):
        return hashlib.sha256(_canonical(self.to_dict()).encode("utf-8")).hexdigest()


class MockOrderStore:
    """SQLite WAL/FULL journal: one order per owner+cart+revision, durable receipts.

    Separate connections plus BEGIN IMMEDIATE serialize threads AND processes.
    Order, receipt and cart checks share one commit; no in-flight external work
    exists, so rollback leaves no uncertain purchase or payment to retry.
    """

    def __init__(self, path):
        self.path = str(path)
        if not self.path or self.path == ":memory:":
            raise CartError("DURABLE_PATH_REQUIRED")
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mock_carts(
                    owner TEXT NOT NULL, cart_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    snapshot TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(owner,cart_id));
                CREATE TABLE IF NOT EXISTS mock_orders(
                    order_id TEXT PRIMARY KEY, owner TEXT NOT NULL, cart_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, body TEXT NOT NULL, UNIQUE(owner,cart_id,revision));
                CREATE TABLE IF NOT EXISTS mock_order_receipts(
                    owner TEXT NOT NULL, cart_id TEXT NOT NULL, key TEXT NOT NULL,
                    revision INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    order_id TEXT NOT NULL REFERENCES mock_orders(order_id),
                    PRIMARY KEY(owner,cart_id,key));
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def save_cart(self, snapshot, *, expected_revision):
        _revision(expected_revision)
        if not isinstance(snapshot, CartSnapshot):
            raise CartError("CART_TYPE")
        if snapshot.revision != expected_revision:
            raise CartError("STALE_REVISION")
        with self._transaction() as db:
            old = db.execute(
                "SELECT revision,sha256,snapshot FROM mock_carts WHERE owner=? AND cart_id=?",
                (snapshot.owner, snapshot.cart_id),
            ).fetchone()
            if old:
                if old[0] > expected_revision:
                    raise CartError("STALE_REVISION")
                if old[0] == expected_revision and old[1] != snapshot.sha256:
                    raise CartError("CART_SNAPSHOT_CHANGED")
                if json.loads(old[2])["trip_plan"]["trip_id"] != snapshot.trip_plan["trip_id"]:
                    raise CartError("CART_TRIP_MISMATCH")
            db.execute(
                "INSERT INTO mock_carts VALUES(?,?,?,?,?) ON CONFLICT(owner,cart_id) "
                "DO UPDATE SET revision=excluded.revision,snapshot=excluded.snapshot,"
                "sha256=excluded.sha256",
                (
                    snapshot.owner,
                    snapshot.cart_id,
                    snapshot.revision,
                    _canonical(snapshot.to_dict()),
                    snapshot.sha256,
                ),
            )
        return snapshot.to_dict()

    def process(self, *, owner, cart_id, expected_revision, idempotency_key):
        """Create a DEMO receipt, or replay it. A changed key is not a new purchase.

        Exact historical replay is read-only even after a cart advances. Reusing
        that key with another revision conflicts; a new stale request is rejected.
        """
        for value in (owner, cart_id, idempotency_key):
            _identifier(value)
        _revision(expected_revision)
        with self._transaction() as db:
            receipt = db.execute(
                "SELECT revision,order_id FROM mock_order_receipts "
                "WHERE owner=? AND cart_id=? AND key=?",
                (owner, cart_id, idempotency_key),
            ).fetchone()
            if receipt:
                if receipt[0] != expected_revision:
                    raise CartError("IDEMPOTENCY_CONFLICT")
                return self._order(db, owner, cart_id, receipt[1])
            cart = db.execute(
                "SELECT revision,snapshot,sha256 FROM mock_carts WHERE owner=? AND cart_id=?",
                (owner, cart_id),
            ).fetchone()
            if not cart:
                raise CartError("CART_NOT_FOUND")
            if cart[0] != expected_revision:
                raise CartError("STALE_REVISION")
            existing = db.execute(
                "SELECT order_id FROM mock_orders WHERE owner=? AND cart_id=? AND revision=?",
                (owner, cart_id, expected_revision),
            ).fetchone()
            if existing:
                order = self._order(db, owner, cart_id, existing[0])
            else:
                order = {
                    "order_id": "demo-" + uuid.uuid4().hex,
                    "mode": "DEMO_ONLY",
                    "status": "DEMO_CREATED",
                    "payment_status": "NOT_PROCESSED",
                    "booking_status": "NOT_BOOKED",
                    "cart": json.loads(cart[1]),
                }
                db.execute(
                    "INSERT INTO mock_orders VALUES(?,?,?,?,?)",
                    (order["order_id"], owner, cart_id, expected_revision, _canonical(order)),
                )
            db.execute(
                "INSERT INTO mock_order_receipts VALUES(?,?,?,?,?,?)",
                (owner, cart_id, idempotency_key, expected_revision, cart[2], order["order_id"]),
            )
            return order

    @staticmethod
    def _order(db, owner, cart_id, order_id):
        row = db.execute(
            "SELECT body FROM mock_orders WHERE owner=? AND cart_id=? AND order_id=?",
            (owner, cart_id, order_id),
        ).fetchone()
        if not row:
            raise CartError("ORDER_NOT_FOUND")
        return json.loads(row[0])

    def get_order(self, *, owner, cart_id, order_id):
        for value in (owner, cart_id, order_id):
            _identifier(value)
        with self._connection() as db:
            return self._order(db, owner, cart_id, order_id)
