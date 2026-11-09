"""Audit metric labels without changing prompts, gold answers, or rerunning a model."""

from copy import deepcopy


def audit(row, case):
    result = deepcopy(row["grade"])
    notes = []
    if row.get("finish_reason") == "length":
        result.update(
            response_complete=False, correct_parse=False, failure="TRUNCATED_RESPONSE_AT_512"
        )
        notes.append("finish_reason=length is truncation even when content is empty")
    parsed = result.get("parsed") or {}
    values = parsed.get("values") or {}
    expected = case["expected"].get("values") or {}
    # Sochi and Сочи denote one city. The strict app contract failure remains;
    # only the false allegation of an invented city is removed.
    if values.get("city") == "Sochi" and expected.get("city") == "Сочи":
        other = {k: v for k, v in values.items() if k not in ("city", "budget")}
        wanted = {k: v for k, v in expected.items() if k not in ("city", "budget")}
        if other == wanted:
            result["claim_flags"] = [
                x for x in result.get("claim_flags", []) if x != "CONDITIONS_NOT_IN_SOURCE_PHRASE"
            ]
            result["unsupported_claims"] = bool(result["claim_flags"])
            result["conditions_preserved"] = True
            notes.append(
                "Known transliteration is a contract/normalization failure, not an invented location; correct_parse and core rejection unchanged"
            )
    result["audit_notes"] = notes
    return result
