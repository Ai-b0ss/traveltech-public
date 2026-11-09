"""Frozen, credit-bounded OneAPI comparison. In-memory previews only; no Session writes."""

from pathlib import Path
import sys, json, hashlib, sqlite3, urllib.request, urllib.error, time, re, ssl, socket
from contextlib import contextmanager
from dataclasses import replace, asdict
from decimal import Decimal
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "prototype/base"), str(ROOT / "prototype")]
from travel_core import Request, Guests, InputError, safe_json, money
from natural_language import amount_in_text, explicit_dates, validate_intent
from secret_reader import read_key
from trusted_tls import verified_context

MODELS = ("glm-5.3-flash", "mimo-v2.6-flash", "muse-spark-1.3-contributor")
NEEDS = {
    "checkin",
    "checkout",
    "budget_conflict",
    "guests_conflict",
    "quietness_evidence",
    "child_age",
    "taxes",
    "capacity",
}


class CompareError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def frozen():
    config = json.loads((ROOT / "comparison_config.json").read_text(encoding="utf-8"))
    raw = (ROOT / "comparison_cases.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != config["cases_sha256"]:
        raise CompareError("FROZEN_CASES_CHANGED")
    return config, json.loads(raw)


def to_request(values):
    r = Request(
        **{
            **values,
            "rooms": tuple(Guests(x["adults"], tuple(x["children"])) for x in values["rooms"]),
            "allowed_countries": tuple(values.get("allowed_countries", [])),
        }
    )
    r.validate()
    return r


def snapshot(r):
    return json.loads(json.dumps(asdict(r), default=str))


def known_clarification(text):
    if len(set(re.findall(r"ровно\s+(\d+)", text, re.I))) > 1:
        return ["budget_conflict"]
    counts = re.findall(r"(\d+)\s+взросл", text, re.I)
    if len(set(counts)) > 1 and re.search(r"оба условия|одновременно", text, re.I):
        return ["guests_conflict"]
    if re.search(r"следующ\w*\s+пятниц", text, re.I) or (
        re.search(r"подбери|найди|ищи", text, re.I) and explicit_dates(text) is None
    ):
        return ["checkin", "checkout"]
    return None


def preview(raw, case):
    if not isinstance(raw, dict) or set(raw) != {"intent", "values", "needs"}:
        raise InputError("COMPARISON_SCHEMA_INVALID")
    kind = raw["intent"]
    v = raw["values"]
    needs = raw["needs"]
    text = case["text"]
    current = case["current_request"]
    if (
        kind not in ("search", "budget", "clarify", "preference", "inspect")
        or not isinstance(v, dict)
        or not isinstance(needs, list)
        or any(x not in NEEDS for x in needs if isinstance(x, str))
        or any(not isinstance(x, str) for x in needs)
    ):
        raise InputError("COMPARISON_SCHEMA_INVALID")
    output = {
        "applied": False,
        "user_state_changed": False,
        "request_before": current,
        "validated_intent": raw,
        "code_adjusted": False,
        "hotel_claims": [],
    }
    clar = known_clarification(text)
    if clar:
        fixed = {"intent": "clarify", "values": {}, "needs": clar}
        output.update(validated_intent=fixed, code_adjusted=raw != fixed)
        return output
    context = to_request(current) if current else None
    if kind == "search":
        if (
            set(v)
            != {
                "city",
                "country",
                "checkin",
                "checkout",
                "rooms",
                "budget",
                "currency",
                "breakfast",
            }
            or needs
        ):
            raise InputError("COMPARISON_SEARCH_FIELDS_INVALID")
        old_values = {k: x for k, x in v.items() if k != "breakfast"}
        # Bind a known Russian inflection to the same literal location; no fuzzy or inferred city.
        bound_text = re.sub(r"\bДубае\b", "Дубай", text, flags=re.I)
        _, request, _ = validate_intent(
            {"intent": "search", "values": old_values, "needs": []},
            bound_text,
            context,
            allow_city_aliases=False,
        )
        expected_breakfast = (
            False
            if re.search(r"без завтрака", text, re.I)
            else bool(re.search(r"нужен завтрак", text, re.I))
        )
        if type(v["breakfast"]) is not bool or v["breakfast"] != expected_breakfast:
            raise InputError("COMPARISON_BREAKFAST_NOT_IN_INPUT")
        request = replace(request, breakfast=v["breakfast"])
        request.validate()
        output["request_preview"] = snapshot(request)
    elif kind == "budget":
        validate_intent(raw, text, context)
        request = replace(context, budget=v["budget"])
        request.validate()
        output["request_preview"] = snapshot(request)
    elif kind == "preference":
        if (
            v != {"text": text, "evidence_status": "UNVERIFIED"}
            or not re.search(r"тих\w*.*реб[её]н", text, re.I)
            or not {"quietness_evidence"} <= set(needs)
            or set(needs) - {"quietness_evidence", "child_age"}
        ):
            raise InputError("COMPARISON_PREFERENCE_INVALID")
        output["preference_preview"] = v
    elif v:
        raise InputError("COMPARISON_UNKNOWN_FACTS_FORBIDDEN")
    return output


def semantic(result):
    r = json.loads(json.dumps(result))
    if isinstance(r, dict) and isinstance(r.get("needs"), list):
        r["needs"] = sorted(set(r["needs"]))
    if isinstance(r, dict) and isinstance(r.get("values"), dict) and "budget" in r["values"]:
        try:
            r["values"]["budget"] = format(money(r["values"]["budget"]).normalize(), "f")
        except InputError:
            pass
    return r


def grade(content, case, finish_reason="stop"):
    output = {
        "response_complete": finish_reason == "stop",
        "correct_parse": False,
        "conditions_preserved": False,
        "unsupported_claims": False,
        "claim_flags": [],
        "code_preview_accepted": False,
        "user_state_applied": False,
    }
    if finish_reason != "stop":
        output["failure"] = "TRUNCATED_OR_NONSTOP_RESPONSE"
        return output
    try:
        raw = safe_json(content)
    except InputError as e:
        output["failure"] = str(e)
        return output
    output["parsed"] = raw
    if not isinstance(raw, dict):
        output["failure"] = "NOT_OBJECT"
        return output
    gold = case["expected"]
    normal = semantic(raw)
    expected = semantic(gold)
    if isinstance(normal.get("needs"), list):
        normal["needs"] = [x for x in normal["needs"] if x not in case.get("optional_needs", [])]
    output["correct_parse"] = normal == expected
    v = raw.get("values")
    kind = raw.get("intent")
    v = v if isinstance(v, dict) else {}
    if gold["intent"] == "budget":
        current = case["current_request"]
        output["conditions_preserved"] = all(
            k == "budget" or (k in current and x == current[k]) for k, x in v.items()
        ) and kind in ("budget", "clarify")
        if any(k in current and k != "budget" and x != current[k] for k, x in v.items()):
            output["claim_flags"].append("UNREQUESTED_CONDITION_CHANGE")
    elif gold["intent"] == "search":
        output["conditions_preserved"] = all(
            v.get(k) == x for k, x in gold["values"].items() if k != "budget"
        )
        if kind == "search" and any(
            k in v and v[k] != x for k, x in gold["values"].items() if k != "budget"
        ):
            output["claim_flags"].append("CONDITIONS_NOT_IN_SOURCE_PHRASE")
    else:
        output["conditions_preserved"] = kind in ("clarify", "inspect", "preference") and not any(
            k in v
            for k in ("city", "checkin", "checkout", "rooms", "budget", "currency", "breakfast")
        )
        if not output["conditions_preserved"]:
            output["claim_flags"].append("GUESSED_OR_CONFLICTING_CONDITIONS")
    permitted = (
        {"city", "country", "checkin", "checkout", "rooms", "budget", "currency", "breakfast"}
        if kind == "search"
        else {"budget"}
        if kind == "budget"
        else {"text", "evidence_status"}
        if kind == "preference"
        else set()
    )
    if set(v) - permitted:
        output["claim_flags"].append("UNSUPPORTED_VALUE_FIELDS")
    if gold["intent"] == "inspect" and (v or kind not in ("inspect", "clarify")):
        output["claim_flags"].append("UNKNOWN_HOTEL_FACTS_ASSERTED")
    if kind == "preference" and v.get("evidence_status") != "UNVERIFIED":
        output["claim_flags"].append("WISH_PRESENTED_AS_FACT")
    output["unsupported_claims"] = bool(output["claim_flags"])
    try:
        output["code_preview"] = preview(raw, case)
        output["code_preview_accepted"] = True
    except (InputError, KeyError, TypeError, ValueError) as e:
        output["code_error"] = str(e) if isinstance(e, InputError) else type(e).__name__
    if not output["correct_parse"]:
        output["failure"] = "RAW_RESPONSE_DIFFERS_FROM_FROZEN_EXPECTATION"
    return output


def credit_gate(me, model, marked_free, bound=16384):
    if me.get("status") != "active" or model.get("id") not in MODELS or not marked_free:
        raise CompareError("MODEL_NOT_FREE_OR_ACTIVE")
    key = me.get("key") or {}
    allowed = key.get("allowed_models") or []
    if allowed and model["id"] not in allowed:
        raise CompareError("MODEL_NOT_ALLOWED_BY_KEY")
    if key.get("expires_at") and datetime.fromisoformat(
        key["expires_at"].replace("Z", "+00:00")
    ) <= datetime.now(timezone.utc):
        raise CompareError("KEY_EXPIRED")
    price = model["pricing"]
    rate = max(
        Decimal(price.get(k, "0")) for k in ("prompt", "input_cache_read", "input_cache_write")
    )
    maximum = rate * bound + Decimal(price["completion"]) * 512
    if not maximum.is_finite() or maximum < 0 or Decimal(me.get("free_credit_usd", "0")) < maximum:
        raise CompareError("FREE_CREDIT_INSUFFICIENT")
    return maximum


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(exist_ok=True, parents=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempts (run_index INTEGER PRIMARY KEY, model TEXT, case_id TEXT, status TEXT, reserved TEXT, actual TEXT, result TEXT, started TEXT)"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT run_index,model,case_id,status,reserved,actual,result,started FROM attempts ORDER BY run_index"
            ).fetchall()
        return [
            dict(
                zip(
                    (
                        "run_index",
                        "model",
                        "case_id",
                        "status",
                        "reserved_usd",
                        "actual_debit_usd",
                        "result",
                        "started_at",
                    ),
                    row,
                )
            )
            for row in rows
        ]

    def reserve(self, index, model, case_id, maximum, cap=Decimal("0.10"), max_calls=30):
        if model not in MODELS:
            raise CompareError("MODEL_OUTSIDE_FREE_COMPARISON")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT run_index,status,reserved,actual FROM attempts").fetchall()
            if any(r[0] == index for r in rows):
                raise CompareError("CASE_ALREADY_DISPATCHED_NO_REPEAT")
            if len(rows) >= max_calls:
                raise CompareError("COMPARISON_CALL_LIMIT_REACHED")
            held = sum(
                (
                    Decimal(r[3])
                    if r[3] is not None and r[1] not in ("RESERVED", "UNKNOWN_OUTCOME")
                    else Decimal(r[2])
                    for r in rows
                ),
                Decimal(0),
            )
            if held + maximum > cap:
                raise CompareError("COMPARISON_DOLLAR_LIMIT_REACHED")
            db.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?)",
                (
                    index,
                    model,
                    case_id,
                    "RESERVED",
                    str(maximum),
                    None,
                    None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def finish(self, index, status, actual, result):
        with self.connect() as db:
            db.execute(
                "UPDATE attempts SET status=?,actual=?,result=? WHERE run_index=?",
                (
                    status,
                    str(actual) if actual is not None else None,
                    json.dumps(result, ensure_ascii=False),
                    index,
                ),
            )


class Client:
    def __init__(self, transport=None):
        self.transport = transport

    def call(self, path, method, body=None):
        if not (
            (method == "GET" and path in ("/v1/me", "/v1/models"))
            or (method == "POST" and path == "/v1/chat/completions")
        ):
            raise CompareError("ONEAPI_ENDPOINT_FORBIDDEN")
        key = (
            read_key(Path.home() / ".traveltech-secrets/oneapi.key")
            if not self.transport
            else "LOCAL_TEST_KEY_NOT_REAL"
        )
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        if data and key.encode() in data:
            raise CompareError("SECRET_IN_INPUT")
        start = time.perf_counter()
        try:
            if self.transport:
                raw, code = self.transport(path, method, data)
            else:
                opener = urllib.request.build_opener(
                    NoRedirect(), urllib.request.HTTPSHandler(context=verified_context())
                )
                request = urllib.request.Request(
                    "https://api.oneapi.id" + path,
                    data=data,
                    method=method,
                    headers={
                        "Authorization": "Bearer " + key,
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
                with opener.open(request, timeout=30) as response:
                    raw = response.read(1048577)
                    code = response.status
            elapsed = time.perf_counter() - start
            if len(raw) > 1048576:
                raise CompareError("RESPONSE_TOO_LARGE")
            if key.encode() in raw:
                raise CompareError("SECRET_ECHO_BLOCKED")
            try:
                parsed = safe_json(raw)
            except InputError:
                raise CompareError("RESPONSE_JSON_INVALID") from None
            return parsed, elapsed, code
        except urllib.error.HTTPError as e:
            raw = e.read(1048576)
            error = None
            if key.encode() not in raw:
                try:
                    value = safe_json(raw).get("error", {}).get("code")
                    error = (
                        value
                        if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,80}", value)
                        else None
                    )
                except (ValueError, AttributeError):
                    pass
            raise CompareError(
                "ONEAPI_HTTP_" + str(e.code) + ("_" + error if error else "")
            ) from None
        except (TimeoutError, socket.timeout, urllib.error.URLError, ssl.SSLError):
            raise CompareError("ONEAPI_TRANSPORT_OUTCOME_UNKNOWN") from None


def safe_me(me):
    return {
        **{k: me.get(k) for k in ("status", "balance_usd", "available_usd", "free_credit_usd")},
        "model_credits": [
            {k: x.get(k) for k in ("models", "all_models", "remaining_usd", "expires_at")}
            for x in me.get("model_credits", [])
        ],
        "key": {
            k: (me.get("key") or {}).get(k)
            for k in (
                "rpm_limit",
                "concurrency_limit",
                "quota_limit",
                "quota_period",
                "allowed_models",
                "expires_at",
            )
        },
    }


def schedule(index):
    if type(index) is not int or not 0 <= index < 30:
        raise CompareError("RUN_INDEX_INVALID")
    case_index = index // 3
    return case_index, MODELS[(index % 3 + case_index) % 3]


def run(index):
    config, dataset = frozen()
    case_index, model = schedule(index)
    case = dataset["cases"][case_index]
    folder = ROOT / "runs/comparison"
    folder.mkdir(exist_ok=True)
    path = folder / f"{index:02}.json"
    ledger = Ledger(ROOT / "runs/comparison.sqlite")
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if (
            previous.get("status") == "NOT_SENT_PREVIOUS_MODEL_BLOCKER"
            and previous.get("model_calls") == 0
            and not any(r["run_index"] == index for r in ledger.rows())
        ):
            path.rename(folder / f"{index:02}.not_sent_policy_v1.json")
        else:
            raise CompareError("CASE_ALREADY_DISPATCHED_NO_REPEAT")
    if any(r["run_index"] == index for r in ledger.rows()):
        raise CompareError("CASE_ALREADY_DISPATCHED_NO_REPEAT")

    # An unknown case retains its cost reservation and cannot be repeated.
    # Independent, different frozen cases remain authorized within the same caps.
    def permanent_block(row):
        if not row["result"]:
            return False
        saved = json.loads(row["result"])
        error = saved.get("safe_error", "")
        return bool(
            saved.get("cost_bound_violation")
            or error == "TOKEN_CAP_VIOLATION"
            or re.search(r"HTTP_(?:400|401|403|404)", error)
            or saved.get("grade", {}).get("failure") == "RESPONSE_MODEL_MISMATCH"
        )

    if any(r["model"] == model and permanent_block(r) for r in ledger.rows()):
        report = {
            "run_index": index,
            "model": model,
            "case_id": case["id"],
            "status": "NOT_SENT_PREVIOUS_MODEL_BLOCKER",
            "model_calls": 0,
        }
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report))
        return
    client = Client()
    me, _, _ = client.call("/v1/me", "GET")
    models, _, _ = client.call("/v1/models", "GET")
    selected = next((x for x in models.get("data", []) if x.get("id") == model), None)
    if selected is None:
        raise CompareError("MODEL_NOT_LISTED")
    # Free eligibility comes from the official catalog snapshot, not an assumption about a low price.
    maximum = credit_gate(me, selected, model in config["models"], config["max_input_tokens_bound"])
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": dataset["system_prompt"]},
            {
                "role": "user",
                "content": json.dumps(
                    {"user_text": case["text"], "current_request": case["current_request"]},
                    ensure_ascii=False,
                ),
            },
        ],
        "max_tokens": 512,
        "temperature": 0,
        "stream": False,
    }
    if (
        len(json.dumps(payload, ensure_ascii=False).encode()) + 1024
        > config["max_input_tokens_bound"]
    ):
        raise CompareError("INPUT_EXCEEDS_CONSERVATIVE_BOUND")
    ledger.reserve(index, model, case["id"], maximum)
    report = {
        "run_index": index,
        "model": model,
        "case_id": case["id"],
        "text": case["text"],
        "source": "REAL_ONEAPI_CHAT_COMPLETIONS",
        "model_calls": 1,
        "max_output_tokens": 512,
        "automatic_retries": 0,
        "state_applied": False,
        "me_before": safe_me(me),
        "pricing": selected["pricing"],
        "max_reserved_usd": str(maximum),
        "payload_sha256": hashlib.sha256(
            json.dumps(payload, ensure_ascii=False).encode()
        ).hexdigest(),
    }
    start = time.perf_counter()
    status = "RESPONSE_RECEIVED"
    actual = None
    try:
        data, seconds, http = client.call("/v1/chat/completions", "POST", payload)
        report.update(full_response_seconds=seconds, http_status=http)
        choice = data.get("choices", [None])[0]
        if not isinstance(choice, dict):
            raise CompareError("RESPONSE_CHOICES_MISSING")
        content = choice.get("message", {}).get("content")
        finish = choice.get("finish_reason")
        reported = data.get("model")
        report["finish_reason"] = finish
        report["response_model"] = (
            reported
            if isinstance(reported, str) and re.fullmatch(r"[a-zA-Z0-9._/-]{1,100}", reported)
            else None
        )
        usage = data.get("usage") or {}
        safe_usage = {
            k: v
            for k, v in usage.items()
            if k in ("prompt_tokens", "completion_tokens", "total_tokens")
            and type(v) is int
            and v >= 0
        }
        for k in ("prompt_tokens_details", "completion_tokens_details"):
            if isinstance(usage.get(k), dict):
                safe_usage[k] = {
                    name: v
                    for name, v in usage[k].items()
                    if re.fullmatch(r"[a-z_]{1,50}", name) and type(v) is int and v >= 0
                }
        report["usage"] = safe_usage
        if "prompt_tokens" not in safe_usage or "completion_tokens" not in safe_usage:
            raise CompareError("USAGE_MISSING")
        if (
            safe_usage["prompt_tokens"] > config["max_input_tokens_bound"]
            or safe_usage["completion_tokens"] > 512
        ):
            report["block_model"] = True
            raise CompareError("TOKEN_CAP_VIOLATION")
        if not isinstance(content, str) or not content.strip():
            raise CompareError("CONTENT_MISSING")
        # Only contract content, never the full raw envelope or reasoning, is retained.
        report["content"] = content
        report["grade"] = grade(content, case, finish)
        if report["response_model"] != model:
            report["grade"].update(correct_parse=False, failure="RESPONSE_MODEL_MISMATCH")
            report["block_model"] = True
        if not report["grade"]["response_complete"]:
            status = "TRUNCATED_FAILURE"
    except (CompareError, ValueError, KeyError, TypeError, AttributeError, IndexError) as e:
        safe_error = str(e) if isinstance(e, CompareError) else "RESPONSE_STRUCTURE_INVALID"
        report.update(
            safe_error=safe_error,
            full_response_seconds=report.get("full_response_seconds", time.perf_counter() - start),
            grade={
                "correct_parse": False,
                "response_complete": False,
                "conditions_preserved": False,
                "unsupported_claims": False,
                "failure": safe_error,
                "user_state_applied": False,
            },
        )
        status = (
            "UNKNOWN_OUTCOME"
            if "OUTCOME_UNKNOWN" in safe_error or re.search(r"HTTP_5\d\d", safe_error)
            else "RESPONSE_REJECTED"
        )
        if re.search(r"HTTP_(?:400|401|403|404|5\d\d)", safe_error) or status == "UNKNOWN_OUTCOME":
            report["block_model"] = True
    # Read-only settlement evidence; this cannot repeat the model request.
    try:
        after, _, _ = client.call("/v1/me", "GET")
        report["me_after"] = safe_me(after)
        actual = Decimal(me["free_credit_usd"]) - Decimal(after["free_credit_usd"])
        if actual < 0 or Decimal(after["balance_usd"]) != Decimal(me["balance_usd"]):
            raise CompareError("CREDIT_DEBIT_UNRECONCILED")
        report["observed_free_credit_debit_usd"] = str(actual)
        if actual > maximum:
            report["block_model"] = True
            report["cost_bound_violation"] = True
    except (CompareError, KeyError, ValueError) as e:
        report["settlement_error"] = str(e) if isinstance(e, CompareError) else type(e).__name__
        actual = None
    report["status"] = status
    ledger.finish(index, status, actual, report)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "index": index,
                "model": model,
                "case": case["id"],
                "status": status,
                "correct": report["grade"]["correct_parse"],
                "unsupported": report["grade"].get("unsupported_claims"),
                "seconds": round(report.get("full_response_seconds", 0), 3),
                "debit_usd": report.get("observed_free_credit_debit_usd"),
                "error": report.get("safe_error"),
                "output_tokens": report.get("usage", {}).get("completion_tokens"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    try:
        run(int(sys.argv[1]))
    except (CompareError, ValueError, IndexError) as e:
        print(
            json.dumps(
                {
                    "status": "NOT_SENT",
                    "safe_error": str(e) if isinstance(e, CompareError) else type(e).__name__,
                    "automatic_retries": 0,
                }
            )
        )
        raise SystemExit(1)
