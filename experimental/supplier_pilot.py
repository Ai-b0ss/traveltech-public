"""Opt-in, no-key Pilot observations; never a payable price or booking client.

Public callable contract and integration limits: docs/research/PILOT_ADAPTER_20261025.md.
Hotel identifiers stay in this client's memory; only local handles leave it.
"""

import copy
import hashlib
import html
import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import deadline_http
import request_deadline
from provider_transport import NoRedirect, canonical, strict_json
from supplier_dispatch import SupplierDispatchJournal
from trusted_tls import verified_context

ENDPOINT = "https://mcp.pilotplans.com/mcp"
MAX_BODY = 512_000
MAX_REQUEST = 16_000
TIMEOUT = 15
MAX_CALLS = 3
WARNINGS = [
    "PRICE_BASIS_UNKNOWN", "TAXES_UNKNOWN", "FEES_UNKNOWN", "EXPIRY_UNKNOWN",
    "OCCUPANCY_UNVERIFIED", "LICENSE_UNCONFIRMED", "NOT_BOOKABLE_QUOTE",
]


class PilotError(ValueError):
    """Safe machine-readable code only; never includes upstream body/URL."""


def validate_query(query):
    """Explicit dates, adults, room count, child ages and nationality; no defaults."""
    required = {"location", "check_in", "check_out", "guests", "rooms",
                "children_ages", "currency", "guest_nationality"}
    if not isinstance(query, dict) or set(query) - (required | {"limit"}) or required - set(query):
        raise PilotError("PILOT_QUERY_FIELDS")
    q = copy.deepcopy(query)
    if (not isinstance(q["location"], str) or not 1 <= len(q["location"].strip()) <= 160
            or any(ord(c) < 32 for c in q["location"])):
        raise PilotError("PILOT_LOCATION")
    try:
        dates = [date.fromisoformat(q[k]) for k in ("check_in", "check_out")]
        if any(d.isoformat() != q[k] for d, k in zip(dates, ("check_in", "check_out"))):
            raise ValueError()
        if dates[0] < date.today() or not 1 <= (dates[1] - dates[0]).days <= 30:
            raise ValueError()
    except (ValueError, TypeError):
        raise PilotError("PILOT_DATES") from None
    for key, upper in (("guests", 8), ("rooms", 4), ("limit", 10)):
        value = q.setdefault(key, 4) if key == "limit" else q[key]
        if type(value) is not int or not 1 <= value <= upper:
            raise PilotError("PILOT_OCCUPANCY_OR_LIMIT")
    if q["rooms"] > q["guests"]:
        raise PilotError("PILOT_OCCUPANCY_OR_LIMIT")
    kids = q["children_ages"]
    if not isinstance(kids, list) or len(kids) > 6 or any(type(x) is not int or not 0 <= x <= 17 for x in kids):
        raise PilotError("PILOT_CHILD_AGES")
    for key, pattern in (("currency", "[A-Z]{3}"), ("guest_nationality", "[A-Z]{2}")):
        if not isinstance(q[key], str) or not re.fullmatch(pattern, q[key]):
            raise PilotError("PILOT_CURRENCY_OR_NATIONALITY")
    return q


def parse_rpc(body, content_type, identifier):
    """One JSON-RPC response, either JSON or the documented single SSE event."""
    if not isinstance(body, bytes) or len(body) > MAX_BODY:
        raise PilotError("PILOT_BODY_LIMIT")
    try:
        text = body.decode("utf-8")
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime == "text/event-stream":
            events, data = [], []
            for line in (text.replace("\r\n", "\n") + "\n\n").split("\n"):
                if not line:
                    if data:
                        events.append("\n".join(data))
                        data = []
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip(" "))
                elif not (line.startswith(":") or line == "event: message"):
                    raise ValueError()
            if len(events) != 1:
                raise ValueError()
            text = events[0]
        elif mime != "application/json":
            raise ValueError()
        result = strict_json(text)
        if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                or type(result.get("id")) is not type(identifier) or result.get("id") != identifier
                or ("result" in result) == ("error" in result)):
            raise ValueError()
        if "error" in result:
            raise PilotError("PILOT_RPC_ERROR")
        if not isinstance(result["result"], dict):
            raise ValueError()
        return result["result"]
    except PilotError:
        raise
    except (ValueError, TypeError, RecursionError):
        raise PilotError("PILOT_RPC_INVALID") from None


def tool_payload(result):
    if "isError" in result and type(result["isError"]) is not bool:
        raise PilotError("PILOT_TOOL_SCHEMA")
    if result.get("isError"):
        raise PilotError("PILOT_TOOL_ERROR")
    payload = result.get("structuredContent")
    if payload is None:
        try:
            content = result["content"]
            if len(content) != 1 or content[0]["type"] != "text":
                raise ValueError()
            payload = strict_json(content[0]["text"])
        except (KeyError, ValueError, TypeError, RecursionError):
            raise PilotError("PILOT_TOOL_SCHEMA") from None
    if not isinstance(payload, dict):
        raise PilotError("PILOT_TOOL_SCHEMA")
    return payload


def safe_text(value):
    # Data only: no HTML, markdown rendering, URLs or supplier instructions executed.
    return html.escape("".join(c for c in value[:300] if ord(c) >= 32), quote=True) if isinstance(value, str) else ""


def observed_amount(value):
    if type(value) not in (str, int, float, Decimal):
        return None
    if len(str(value)) > 64:
        return None
    try:
        amount = Decimal(str(value))
        if (not amount.is_finite() or not 0 <= amount <= Decimal("999999999")
                or not -24 <= amount.as_tuple().exponent <= 9):
            return None
        return format(amount, "f")
    except (InvalidOperation, ValueError):
        return None


def normalize(payload, query, observed_at, *, kind="search", mode="TEST_FIXTURE", name=""):
    """Pure parser. Fixture/snapshot inputs never become LIVE by default.

    Amount is deliberately null; observed_amount preserves the supplier's number
    without rounding, multiplying nights, guessing taxes or using request currency.
    """
    if kind not in ("search", "rates") or mode not in ("LIVE", "TEST_FIXTURE", "CACHED"):
        raise PilotError("PILOT_NORMALIZE_MODE")
    try:
        stamp = datetime.fromisoformat(observed_at)
        if stamp.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise PilotError("PILOT_OBSERVED_AT") from None
    field = "hotels" if kind == "search" else "offers"
    rows = payload.get(field) if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > (50 if kind == "search" else 100):
        raise PilotError("PILOT_COLLECTION_SCHEMA")
    output = []
    for row in rows:
        if not isinstance(row, dict):
            raise PilotError("PILOT_ROW_SCHEMA")
        currency = row.get("currency", payload.get("currency"))
        if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
            currency = None
        value = observed_amount(row.get("price"))
        warnings = list(WARNINGS)
        if value is None:
            warnings.append("PRICE_UNKNOWN")
        if currency is None:
            warnings.append("CURRENCY_UNKNOWN")
        elif currency != query["currency"]:
            warnings.append("CURRENCY_MISMATCH")
        if kind == "rates":
            warnings.append("RATE_CONTEXT_UNVERIFIED")
        if mode != "LIVE":
            warnings.append("NOT_LIVE")
        output.append({
            "name": safe_text(row.get("name") if kind == "search" else name),
            "room_name": safe_text(row.get("roomName")) if kind == "rates" else "",
            "source": "Pilot", "provider": "Pilot", "source_mode": mode,
            "observation_type": "SEARCH_OBSERVATION", "observation_kind": kind,
            "quote_status": "UNVERIFIED", "observed_at": observed_at,
            "expires_at": None, "expiry_status": "UNKNOWN", "price_basis": "UNKNOWN",
            "freshness_status": "UNKNOWN",
            "tax_status": "UNKNOWN", "fee_status": "UNKNOWN",
            "amount": None, "observed_amount": value, "currency": currency,
            "eligible_for_verified_total": False, "availability": "SEARCH_ONLY",
            "terms_status": "PENDING", "source_reference": ENDPOINT,
            "checkin": query["check_in"], "checkout": query["check_out"],
            "requested_occupancy": {k: copy.deepcopy(query[k]) for k in
                                    ("guests", "rooms", "children_ages", "guest_nationality")},
            "occupancy_status": "UNVERIFIED", "trust": "untrusted_supplier_data",
            "refundable": row.get("refundable") if type(row.get("refundable")) is bool else "UNKNOWN",
            "cancellation": "UNKNOWN", "board": safe_text(row.get("board")),
            "warnings": warnings,
        })
    return output


def to_trip_plan_hotel(observation):
    """Additive bridge: caller must set total_amount=null for this unknown hotel.

    No provider IDs, booking links or raw content cross this boundary.
    """
    return {
        "name": observation["name"], "checkin": observation["checkin"],
        "checkout": observation["checkout"], "amount": None,
        "currency": observation["currency"], "source_mode": observation["source_mode"],
        "provider": "Pilot", "observation": copy.deepcopy(observation),
    }


class PilotClient:
    """One owner/request scope, at most 3 HTTP calls, durable no-retry receipts.

    Scope IDs must be internal, stable and non-personal. Use a fresh request_id
    only for a deliberate new search; never rotate it to retry unknown outcomes.
    """

    def __init__(self, state_dir, *, owner, request_id):
        if not all(isinstance(x, str) and 8 <= len(x) <= 128 for x in (owner, request_id)):
            raise PilotError("PILOT_SCOPE_REQUIRED")
        self.owner, self.request_id = owner, request_id
        self.state_dir = Path(state_dir)
        self._calls = 0
        self._hotels = {}
        self._lock = threading.Lock()

    def _post(self, raw):
        request = urllib.request.Request(ENDPOINT, data=raw, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        })
        # No auth/cookies/proxy credentials, no redirect and no environmental endpoint.
        opener = deadline_http.opener(NoRedirect(), urllib.request.ProxyHandler({}), context=verified_context())
        with request_deadline.budget(TIMEOUT):
            with opener.open(request, timeout=request_deadline.timeout(TIMEOUT)) as response:
                if response.status != 200:
                    raise PilotError("PILOT_HTTP_" + str(response.status))
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise PilotError("PILOT_ENCODING_REFUSED")
                size = response.headers.get("Content-Length")
                if size is not None and (not size.isdigit() or int(size) > MAX_BODY):
                    raise PilotError("PILOT_BODY_LIMIT")
                body = request_deadline.read(response, MAX_BODY + 1)
                request_deadline.check()
                return body, response.headers.get("Content-Type", "")

    def _rpc(self, method, params=None):
        if os.environ.get("TRAVELTECH_PILOT_ENABLED") != "1":
            raise PilotError("PILOT_DISABLED")
        if method != "tools/list" and not (method == "tools/call" and isinstance(params, dict)
                and params.get("name") in ("search_hotels", "get_room_rates_for_hotel")):
            raise PilotError("PILOT_METHOD_REFUSED")
        with self._lock:
            if self._calls >= MAX_CALLS:
                raise PilotError("PILOT_CALL_LIMIT")
            identifier = self._calls + 1
            envelope = {"jsonrpc": "2.0", "id": identifier, "method": method}
            if params is not None:
                envelope["params"] = params
            raw = canonical(envelope).encode("utf-8")
            if len(raw) > MAX_REQUEST:
                raise PilotError("PILOT_REQUEST_LIMIT")
            self.state_dir.mkdir(parents=True, exist_ok=True)
            journal = SupplierDispatchJournal(self.state_dir)
            # Hash semantic request, excluding RPC sequence ID, to block unknown replay.
            digest = hashlib.sha256(canonical([method, params]).encode()).hexdigest()
            try:
                receipt = journal.reserve(self.owner, "pilot:" + self.request_id, identifier, "pilot_mcp", digest)
            except ValueError:
                raise PilotError("PILOT_PRIOR_DISPATCH_NO_RETRY") from None
            self._calls += 1
            try:
                body, mime = self._post(raw)
                result = parse_rpc(body, mime, identifier)
            except Exception as exc:
                journal.finish(receipt, False, error_type="PILOT_UPSTREAM_ERROR")
                if isinstance(exc, PilotError):
                    raise
                if isinstance(exc, urllib.error.HTTPError):
                    code = "PILOT_HTTP_" + str(exc.code)
                    exc.close()
                elif isinstance(exc, TimeoutError):
                    code = "PILOT_TIMEOUT_UNKNOWN_OUTCOME"
                else:
                    code = "PILOT_UPSTREAM_UNKNOWN_OUTCOME"
                raise PilotError(code) from None
            journal.finish(receipt, True)  # No raw supplier data persisted.
            return result

    def list_tools(self):
        result = self._rpc("tools/list")
        tools = result.get("tools")
        if not isinstance(tools, list) or len(tools) > 20 or any(not isinstance(t, dict) for t in tools):
            raise PilotError("PILOT_TOOLS_SCHEMA")
        return [t["name"] for t in tools if t.get("name") in ("search_hotels", "get_room_rates_for_hotel")]

    def search(self, query):
        q = validate_query(query)
        payload = tool_payload(self._rpc("tools/call", {"name": "search_hotels", "arguments": q}))
        stamp = datetime.now(timezone.utc).isoformat()
        observations = normalize(payload, q, stamp, mode="LIVE")
        for observation, row in zip(observations, payload["hotels"]):
            hid = row.get("id")
            if isinstance(hid, str) and 1 <= len(hid) <= 8192:
                handle = uuid.uuid4().hex
                self._hotels[handle] = (hid, q, observation["name"], observation["observed_amount"], observation["currency"])
                observation["research_handle"] = handle
        return {"status": "OK" if observations else "NO_OFFERS", "observations": observations,
                "source": "Pilot", "source_mode": "LIVE", "observed_at": stamp}

    def room_rates(self, handle):
        if not isinstance(handle, str) or handle not in self._hotels:
            raise PilotError("PILOT_HANDLE_UNKNOWN")
        hid, q, name, search_amount, search_currency = self._hotels[handle]
        # The rate tool cannot express rooms/children/currency/nationality. Do not
        # silently drop a family or multiple rooms; single-room context is unverified.
        if q["rooms"] != 1 or q["children_ages"]:
            raise PilotError("PILOT_RATE_OCCUPANCY_UNSUPPORTED")
        arguments = {"hotel_id": hid, **{k: q[k] for k in ("check_in", "check_out", "guests")}}
        payload = tool_payload(self._rpc("tools/call", {"name": "get_room_rates_for_hotel", "arguments": arguments}))
        if "hotelId" in payload and payload["hotelId"] != hid:
            raise PilotError("PILOT_RATE_HOTEL_MISMATCH")
        stamp = datetime.now(timezone.utc).isoformat()
        # Name was escaped at search; avoid double escaping.
        observations = normalize(payload, q, stamp, kind="rates", mode="LIVE", name=html.unescape(name))
        for observation in observations:
            if (search_amount is not None and observation["observed_amount"] is not None
                    and observation["currency"] == search_currency
                    and Decimal(search_amount) != Decimal(observation["observed_amount"])):
                observation["warnings"].append("SEARCH_RATE_DIFFER")
        return {"status": "OK" if observations else "NO_OFFERS", "observations": observations,
                "source": "Pilot", "source_mode": "LIVE", "observed_at": stamp}
