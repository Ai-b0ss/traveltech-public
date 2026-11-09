"""Persistent, narrowly scoped owner-accepted historical IISHKO uncertainty.
The original UNKNOWN_OUTCOME rows are never edited or deleted.
A different, independent inference is allowed only while the accepted rows
match exactly; a newly unknown IISHKO request closes the route automatically.
"""

import hashlib, time
from provider_transport import Failure, canonical
from .model_policy import blocked_reason, scope


def digest(row):
    return hashlib.sha256(canonical(row).encode("utf-8")).hexdigest()


def accepted_ids(rows, policy):
    if not isinstance(policy, dict) or policy.get("enabled") is not True:
        return set()
    accept = policy.get("accepted_unknown_sha256", {})
    if not isinstance(accept, dict):
        return set()
    result = set()
    for row in rows:
        if (
            row.get("provider") == "IISHKO"
            and row.get("state") == "UNKNOWN_OUTCOME"
            and isinstance(row.get("id"), str)
            and accept.get(row["id"]) == digest(row)
        ):
            result.add(row["id"])
    return result


def reason(rows, route, op_hash=None, policy=None):
    # No exact unknown request can ever be replayed, under any provider route.
    if op_hash and any(
        r.get("state") in ("RESERVED", "UNKNOWN_OUTCOME") and r.get("operation_hash") == op_hash
        for r in rows
    ):
        return "UNKNOWN_OPERATION_NO_REPLAY"
    if route != "IISHKO":
        return blocked_reason(rows, route, op_hash)
    accepted = accepted_ids(rows, policy)
    effective = []
    for r in rows:
        if r.get("id") in accepted and r.get("provider") == "IISHKO":
            effective.append({**r, "state": "OWNER_ACCEPTED_HISTORICAL_UNCERTAINTY"})
        else:
            effective.append(r)
    return blocked_reason(effective, route, op_hash)


def new_policy(rows, owner_instruction, evidence):
    if not isinstance(owner_instruction, str) or not owner_instruction.strip():
        raise Failure("OWNER_INSTRUCTION_REQUIRED")
    if (
        not isinstance(evidence, dict)
        or evidence.get("confirmed_answers", 0) < 5
        or evidence.get("sent", 0) < 5
    ):
        raise Failure("REQUALIFICATION_EVIDENCE_REQUIRED")
    if any(r.get("provider") == "IISHKO" and r.get("state") == "RESERVED" for r in rows):
        raise Failure("IISHKO_ACTIVE_RESERVATION")
    accepted = {
        r["id"]: digest(r)
        for r in rows
        if r.get("provider") == "IISHKO"
        and r.get("state") == "UNKNOWN_OUTCOME"
        and isinstance(r.get("id"), str)
    }
    return {
        "schema": 1,
        "enabled": True,
        "route": "IISHKO",
        "accepted_unknown_sha256": accepted,
        "authorized_at": time.time(),
        "owner_instruction": owner_instruction,
        "qualification_evidence": evidence,
        "historical_outcomes_reconciled": False,
        "automatic_redispatch": False,
    }
