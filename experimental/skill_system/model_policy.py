"""Shared account quarantine and terminal response policy; never redispatch a run."""

import hashlib, math, time
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from provider_transport import canonical

UNKNOWN = {"RESERVED", "UNKNOWN_OUTCOME"}

# This capability is set only inside a verified, read-only Skills inference
# fallback. It is isolated per thread and never applies to supplier operations.
_READONLY_RECOVERY = ContextVar("traveltech_readonly_inference_recovery", default=None)


@contextmanager
def authorized_readonly_recovery(proof):
    token = _READONLY_RECOVERY.set(proof)
    try:
        yield
    finally:
        _READONLY_RECOVERY.reset(token)


def read_only_recovery_proof():
    return _READONLY_RECOVERY.get()


def run_key(tag):
    return tag.split(":", 1)[0] if isinstance(tag, str) and tag else None


def scope(route):
    if not isinstance(route, str):
        return "UNKNOWN"
    if route.startswith("Logicc"):
        return "Logicc"
    if route.startswith("InkStone"):
        return "InkStone"
    if route in ("OneAPI", "Jev"):
        return "OneAPI"
    return route


def row_scope(row):
    return row.get("account_scope") or scope(
        row.get("route") or row.get("provider") or row.get("group")
    )


def operation_hash(messages, tools):
    return hashlib.sha256(canonical([messages, tools]).encode()).hexdigest()


def retry_seconds(value, at=None, default=60):
    at = time.time() if at is None else at
    try:
        delay = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - at
        except (TypeError, ValueError, OverflowError, IndexError):
            delay = default
    if not math.isfinite(delay):
        delay = default
    return min(30 * 86400, max(1, delay))


def blocked_reason(rows, route, op_hash=None, at=None):
    at = time.time() if at is None else at
    group = scope(route)
    for r in rows:
        if r.get("state") in UNKNOWN:
            if op_hash and r.get("operation_hash") == op_hash:
                return "UNKNOWN_OPERATION_NO_REPLAY"
            if row_scope(r) == group:
                return "UNRESOLVED_ACCOUNT_OUTCOME"
    same = [r for r in rows if row_scope(r) == group]
    if any(r.get("http") in (401, 402, 403) for r in same):
        return "ACCOUNT_AUTH_STOP"
    if any(r.get("retry_at", 0) > at for r in same):
        return "PROVIDER_COOLDOWN"
    for r in same:
        if r.get("http") in (500, 502, 503, 504) and not r.get("retry_at"):
            try:
                if datetime.fromisoformat(r["at"]).timestamp() + 30 > at:
                    return "PROVIDER_COOLDOWN"
            except (KeyError, ValueError, TypeError):
                return "PROVIDER_CIRCUIT_AFTER_HTTP_FAILURE"
    return None


# IISHKO returns the upstream DeepSeek identifier for this exact requested alias.
REPORTED_MODEL_ALIASES = {
    "deepseek-v4.1-flash": frozenset({"deepseek-ai/DeepSeek-V4.1-Flash"}),
}


def response_record(data, requested):
    # A complete HTTP reply is a known outcome even when unusable by the agent.
    if not isinstance(data, dict):
        return {"response_usable": False, "error": "INFERENCE_ENVELOPE_INVALID"}
    model = data.get("model")
    out = {
        "reported_model": model,
        "identity_attested": False,
        "identity_status": "UNREPORTED",
        "usage": data.get("usage") if isinstance(data.get("usage"), dict) else {},
        "remote_id": data.get("id"),
    }
    if model is not None:
        valid_alias = model in REPORTED_MODEL_ALIASES.get(requested, ())
        if model != requested and not valid_alias:
            return {
                **out,
                "response_usable": False,
                "identity_status": "MISMATCH",
                "error": "MODEL_IDENTITY_MISMATCH",
            }
        out.update(
            identity_attested=True,
            identity_status="MATCHED_VERIFIED_ALIAS" if valid_alias else "MATCHED_RESPONSE_LABEL",
        )
    choices = data.get("choices")
    valid = (
        isinstance(choices, list)
        and bool(choices)
        and all(isinstance(c, dict) and isinstance(c.get("message"), dict) for c in choices)
    )
    if not valid:
        return {**out, "response_usable": False, "error": "INFERENCE_ENVELOPE_INVALID"}
    return {**out, "choices": choices, "response_usable": True}


def compact_record(record, at=None, processed_ttl=3600, unprocessed_ttl=7 * 86400):
    at = time.time() if at is None else at
    if record.get("state") in UNKNOWN:
        return record
    try:
        created = datetime.fromisoformat(record["at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return record
    processed = record.get("application_processed_at")
    expires = (
        (processed + processed_ttl)
        if isinstance(processed, (int, float))
        else created + unprocessed_ttl
    )
    if at < expires:
        return record
    result = dict(record)
    for key in ("choices", "request", "systemone"):
        if key in result:
            result[key + "_sha256"] = hashlib.sha256(
                canonical(result.pop(key)).encode()
            ).hexdigest()
    if result != record:
        result["payload_state"] = "EXPIRED_PROCESSED" if processed else "EXPIRED_UNACKNOWLEDGED"
    return result
