"""One set of JSON contracts for prompts, tools and local enforcement."""

import math
from provider_transport import Failure, strict_json
def completed(record):
    return isinstance(record, dict) and record.get("state") == "RESPONSE_RECEIVED" and record.get("http") == 200



class ContractError(Failure):
    pass


def validate(value, schema, path="$"):
    typ = schema.get("type")
    good = {
        "object": lambda: type(value) is dict,
        "array": lambda: type(value) is list,
        "string": lambda: type(value) is str,
        "boolean": lambda: type(value) is bool,
        "integer": lambda: type(value) is int,
        "number": lambda: type(value) in (int, float) and math.isfinite(value),
        "null": lambda: value is None,
    }
    if typ and not good[typ]():
        raise ContractError(path + ":TYPE_" + typ)
    if "enum" in schema and not any(type(value) is type(x) and value == x for x in schema["enum"]):
        raise ContractError(path + ":ENUM")
    if type(value) is dict:
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value):
            raise ContractError(path + ":MISSING_FIELDS")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            forbidden = sorted(set(value) - set(props))
            raise ContractError(path + ":EXTRA_FIELDS:" + ",".join(forbidden[:8]))
        for k, v in value.items():
            sub = props.get(k, schema.get("additionalProperties", {}))
            if type(sub) is dict:
                validate(v, sub, path + "." + k)
    if type(value) is list:
        if len(value) > schema.get("maxItems", 10000):
            raise ContractError(path + ":TOO_MANY_ITEMS")
        if schema.get("uniqueItems") and len({repr(x) for x in value}) != len(value):
            raise ContractError(path + ":DUPLICATES")
        for i, v in enumerate(value):
            validate(v, schema.get("items", {}), path + "[" + str(i) + "]")
    if type(value) is str and len(value) > schema.get("maxLength", 100000):
        raise ContractError(path + ":STRING_LIMIT")
    if type(value) in (int, float):
        if (
            not math.isfinite(value)
            or value < schema.get("minimum", -math.inf)
            or value > schema.get("maximum", math.inf)
        ):
            raise ContractError(path + ":NUMBER_RANGE")
    return value


STRING = {"type": "string", "maxLength": 1500}
ROOM = {
    "type": "object",
    "properties": {
        "adults": {"type": "integer", "minimum": 1, "maximum": 20},
        "children": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "integer", "minimum": 0, "maximum": 17},
        },
    },
    "required": ["adults", "children"],
    "additionalProperties": False,
}
VALUE_PROPS = {
    k: STRING
    for k in (
        "city",
        "country",
        "checkin",
        "checkout",
        "budget",
        "currency",
        "text",
        "breakfast_wish",
    )
}
VALUE_PROPS.update(
    breakfast={"type": "boolean"},
    rooms={"type": "array", "maxItems": 10, "items": ROOM},
    evidence_status={"type": "string", "enum": ["UNVERIFIED"]},
)
INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [
                "search",
                "budget",
                "constraints",
                "refine",
                "undo",
                "inspect",
                "clarify",
                "preference",
            ],
        },
        "values": {"type": "object", "properties": VALUE_PROPS, "additionalProperties": False},
        "needs": {"type": "array", "maxItems": 12, "uniqueItems": True, "items": STRING},
        "date_error": STRING,
        "city_candidate": STRING,
        "evidence": {"type": "object", "additionalProperties": STRING},
    },
    "required": ["intent", "values", "needs", "evidence"],
    "additionalProperties": False,
}


def presentation_schema(offers, facts):
    return {
        "type": "object",
        "properties": {
            "offer_ids": {
                "type": "array",
                "maxItems": 3,
                "uniqueItems": True,
                "items": {"type": "string", "enum": offers},
            },
            "reason_ids": {
                "type": "array",
                "maxItems": 12,
                "uniqueItems": True,
                "items": {"type": "string", "enum": facts},
            },
        },
        "required": ["offer_ids", "reason_ids"],
        "additionalProperties": False,
    }


def tool(name, description, schema):
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }


def decode(receipt, name, schema):
    if not completed(receipt):
        raise Failure(receipt.get("error") or receipt.get("state") or "TRANSPORT_FAILED")
    choices = receipt.get("choices", [])
    if len(choices) != 1:
        raise ContractError("ONE_CHOICE_REQUIRED")
    c = choices[0]
    msg = c.get("message", {})
    if (
        c.get("finish_reason") not in ("stop", "tool_calls")
        or msg.get("role") != "assistant"
        or msg.get("refusal")
    ):
        raise ContractError("INCOMPLETE_OR_REFUSED")
    calls = msg.get("tool_calls") or []
    if calls:
        if (
            len(calls) != 1
            or calls[0].get("type") != "function"
            or calls[0].get("function", {}).get("name") != name
        ):
            raise ContractError("UNEXPECTED_TOOL")
        raw = calls[0]["function"].get("arguments")
        mode = "NATIVE_TOOL"
    else:
        raw = msg.get("content")
        mode = "JSON_CONTENT"
    if not isinstance(raw, str) or not raw.strip():
        raise ContractError("EMPTY_RESPONSE")
    # Exactly one JSON object; no guessing, wrapper unwrapping or numeric coercion.
    try:
        value = strict_json(raw)
    except Exception as e:
        raise ContractError("INVALID_JSON") from e
    # Narrow, auditable normalization for a control enum only. Never coerce dates,
    # money, identifiers or other user/business values.
    if name == "propose_intent" and type(value) is dict:
        raw_intent = value.get("intent")
        allowed = schema.get("properties", {}).get("intent", {}).get("enum", [])
        if (
            type(raw_intent) is str
            and len(raw_intent) >= 3
            and raw_intent[0] == '"'
            and raw_intent[-1] == '"'
        ):
            candidate = raw_intent[1:-1]
            if candidate in allowed:
                value = {**value, "intent": candidate}
                mode += "_NORMALIZED_CONTROL_ENUM"
    return validate(value, schema), mode
