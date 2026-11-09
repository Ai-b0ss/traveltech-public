"""Product inference ledger, real entitlements and shared account rate slots; no retries."""

import deadline_http
import request_deadline as deadline
import hashlib, json, os, sqlite3, time, urllib.request, urllib.error, uuid
from contextlib import closing
from decimal import Decimal as D
from pathlib import Path
from provider_transport import (
    CreditTransport,
    Failure,
    NoRedirect,
    ROUTES,
    strict_json,
    canonical,
    now,
)
from inkstone_transport import read_dpapi, RATES, SOURCE
from secret_reader import read_key
from .inkstone_price_evidence import verify_model_price
from trusted_tls import verified_context
from .model_policy import (
    scope,
    run_key,
    operation_hash,
    retry_seconds,
    response_record,
    compact_record,
    blocked_reason,
    read_only_recovery_proof,
)
from .iishko_requalification import reason as iishko_reason, new_policy, accepted_ids

ROOT = Path(__file__).resolve().parents[2]
PROFILES = {
    "Logicc_GPT6": ("Logicc", "gpt-6.1-sol"),
    "Logicc_Opus": ("Logicc", "claude-5.5-opus"),
    "Logicc_Sonnet": ("Logicc", "claude-5.5-sonnet"),
    "IISHKO": ("IISHKO", "deepseek-v4.1-flash"),
    "Gwarden_Gemini4Argon": ("Gwarden", "gemini-4-argon"),
    "Pollinations": ("Pollinations", "openai/gpt-6-luna"),
    "OneAPI": ("OneAPI", "muse-spark-1.3-contributor"),
    "InkStone": ("InkStone", "Atria-Dawn-Preview"),
    "InkStone_InternS2": ("InkStone", "intern-s2"),
    "InkStone_AgentsA1": ("InkStone", "Agents-A1"),
    "InkStone_GLM": ("InkStone", "glm-5.3"),
    "InkStone_MiniMax": ("InkStone", "minimax-m3"),
    "InkStone_Qwen": ("InkStone", "qwen3.8-27b"),
    "InkStone_DeepSeek": ("InkStone", "deepseek-v4-flash-0731"),
    "InkStone_Vision": ("InkStone", "deepseek-v4-flash-vision"),
    "InkStone_Kimi": ("InkStone", "kimi-k2.6"),
}



def gwarden_configuration_reason():
    """The limited-time free promotion must be explicitly confirmed by the owner."""
    folder = Path(os.environ.get("TRAVELTECH_SECRETS_DIR") or Path.home() / ".traveltech-secrets")
    if not os.environ.get("GWARDEN_API_KEY") and not (folder / "gwarden.key").is_file():
        return "GWARDEN_KEY_MISSING"
    if os.environ.get("TRAVELTECH_GWARDEN_FREE_PROMO_CONFIRMED") != "1":
        return "GWARDEN_FREE_PROMO_NOT_CONFIRMED"
    deadline = os.environ.get("TRAVELTECH_GWARDEN_FREE_PROMO_EXPIRES_AT", "")
    if not deadline:
        return "GWARDEN_PROMO_EXPIRY_REQUIRED"
    try:
        left = int(deadline) - time.time()
    except ValueError:
        return "GWARDEN_PROMO_EXPIRY_INVALID"
    if left <= 0:
        return "GWARDEN_PROMO_EXPIRED"
    if left > 86400:
        return "GWARDEN_PROMO_EXPIRY_TOO_DISTANT"
    return None


def gwarden_key():
    problem = gwarden_configuration_reason()
    if problem:
        raise Failure(problem)
    value = os.environ.get("GWARDEN_API_KEY")
    if not value:
        folder = Path(os.environ.get("TRAVELTECH_SECRETS_DIR") or Path.home() / ".traveltech-secrets")
        value = read_key(folder / "gwarden.key")
    if not 8 <= len(value) <= 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise Failure("GWARDEN_KEY_INVALID")
    return value


class SkillTransport:
    def __init__(self, state, network=None, rate_path=None):
        self.state = Path(state)
        self.state.mkdir(exist_ok=True, parents=True)
        self.network = network
        self.opener = deadline_http.opener(NoRedirect(), context=verified_context())
        self.legacy = CreditTransport(self.state / "account_metadata", network=network)
        self.prices = {}
        self.proofs = {}
        self.rate_path = Path(
            rate_path
            or (
                Path(os.environ.get("TRAVELTECH_SECRETS_DIR") or Path.home() / ".traveltech-secrets") / "traveltech_rate_slots.sqlite"
                if os.name == "nt" and network is None
                else self.state / "rate_slots.sqlite"
            )
        )
        self.rate_path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,tag TEXT UNIQUE,route TEXT,state TEXT,record TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS preflight_health(scope TEXT PRIMARY KEY,reason TEXT,until_unix REAL,checked_at REAL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_scope_resumption(scope TEXT PRIMARY KEY,record TEXT NOT NULL)"
            )
        with closing(sqlite3.connect(self.rate_path, timeout=60, isolation_level=None)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS slots(scope TEXT PRIMARY KEY,next REAL)")

    def db(self):
        return closing(
            sqlite3.connect(self.state / "requests.sqlite", timeout=60, isolation_level=None)
        )

    def rows(self):
        with self.db() as db:
            rows = [
                strict_json(x[0]) for x in db.execute("SELECT record FROM requests ORDER BY rowid")
            ]
        if (self.state / "logicc_native/logicc.sqlite").exists():
            from logicc_transport import LogiccTransport

            rows += LogiccTransport(self.state / "logicc_native").rows()
        return rows

    def _iishko_policy(self, db=None):
        if db is not None:
            row = db.execute(
                "SELECT record FROM model_scope_resumption WHERE scope=?", ("IISHKO",)
            ).fetchone()
            return strict_json(row[0]) if row else {}
        with self.db() as conn:
            return self._iishko_policy(conn)

    def authorize_iishko_new_runs(self, owner_instruction, evidence):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [strict_json(x[0]) for x in db.execute("SELECT record FROM requests")]
            policy = new_policy(rows, owner_instruction, evidence)
            db.execute(
                "INSERT OR REPLACE INTO model_scope_resumption VALUES(?,?)",
                ("IISHKO", canonical(policy)),
            )
            db.commit()
        return policy

    def route_configuration_reason(self, route):
        return gwarden_configuration_reason() if route == "Gwarden_Gemini4Argon" else None

    def route_blocked_reason(self, rows, route, op_hash=None):
        return iishko_reason(
            rows, route, op_hash, self._iishko_policy() if route == "IISHKO" else None
        )

    def acknowledge_run(self, key):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for ident, raw in db.execute("SELECT id,record FROM requests").fetchall():
                r = strict_json(raw)
                if run_key(r.get("tag")) == key and r.get("state") == "RESPONSE_RECEIVED":
                    r.setdefault("application_processed_at", time.time())
                    db.execute("UPDATE requests SET record=? WHERE id=?", (canonical(r), ident))
            db.commit()

    def purge_payloads(self, at=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for ident, raw in db.execute("SELECT id,record FROM requests").fetchall():
                r = strict_json(raw)
                new = compact_record(r, at)
                if new != r:
                    db.execute("UPDATE requests SET record=? WHERE id=?", (canonical(new), ident))
            db.commit()

    def prior_guards(self, p, model):
        base = "OneAPI" if p == "Jev" else p
        guard = ROOT / "ROUTE_GUARDS.json"
        # Muse/OneAPI's unresolved request remains blocked, but a new, distinct
        # System One/Jev model and endpoint do not replay that old operation.
        if (
            guard.exists()
            and base in json.loads(guard.read_text(encoding="utf-8")).get("blocked", {})
            and p != "Jev"
        ):
            raise Failure("UNRESOLVED_PRIOR_OUTCOME_ROUTE_BLOCKED")
        # Never hide the old unresolved routes by choosing a fresh state directory.
        candidates = [
            (self.state.parent / "credit.sqlite", "calls"),
            (self.state.parent / "inkstone/inkstone.sqlite", "requests"),
            (ROOT / "budget_state/credit.sqlite", "calls"),
            (ROOT / "budget_state/inkstone/inkstone.sqlite", "requests"),
            (
                ROOT.parent.parent / "TravelTech_FULL_PRODUCT_20261015/credit_state/credit.sqlite",
                "calls",
            ),
            (
                ROOT.parent.parent / "TravelTech_INKSTONE_20261015/ledger/inkstone.sqlite",
                "requests",
            ),
        ]
        for path, table in candidates:
            if not path.exists():
                continue
            with closing(sqlite3.connect(path, timeout=30)) as db:
                for (raw,) in db.execute("SELECT record FROM " + table):
                    r = strict_json(raw)
                    same = (
                        (
                            str(r.get("provider", "")).startswith("InkStone")
                            or r.get("model") in RATES
                        )
                        if p.startswith("InkStone")
                        else r.get("model") == model
                        if p == "Jev"
                        else r.get("provider") == base
                    )
                    if r.get("state") in ("RESERVED", "UNKNOWN_OUTCOME") and same:
                        raise Failure("PRIOR_UNKNOWN_NO_RETRY")

    def health_reason(self, p):
        with self.db() as db:
            row = db.execute(
                "SELECT reason,until_unix FROM preflight_health WHERE scope=?", (scope(p),)
            ).fetchone()
        return row[0] if row and row[1] > time.time() else None

    def preflight(self, p):
        try:
            result = self._preflight(p)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = (
                "PROVIDER_PREFLIGHT_HTTP_ERROR"
                if isinstance(exc, urllib.error.HTTPError)
                else "PROVIDER_PREFLIGHT_UNAVAILABLE"
            )
            delay = retry_seconds(
                exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None,
                default=300 if getattr(exc, "code", None) in (401, 402, 403) else 30,
            )
            with self.db() as db:
                db.execute(
                    "INSERT OR REPLACE INTO preflight_health VALUES(?,?,?,?)",
                    (scope(p), reason, time.time() + delay, time.time()),
                )
            raise Failure(reason) from exc
        with self.db() as db:
            db.execute("DELETE FROM preflight_health WHERE scope=?", (scope(p),))
        return result

    def _preflight(self, p):
        if p not in PROFILES and p != "Jev":
            raise Failure("SKILL_ROUTE_UNKNOWN")
        group, model = PROFILES[p] if p != "Jev" else ("OneAPI", "jev-1.13")
        if group == "Logicc":
            from logicc_transport import LogiccTransport

            return LogiccTransport(self.state / "logicc_native", model=model).check_agent_capacity(
                p
            )
        self.prior_guards(p, model)
        if self.network:
            self.prices[p] = (D("0"), D("0"))
            self.proofs[p] = {"at": time.time(), "mode": "FAKE_TEST"}
            return
        if group == "Gwarden":
            key = gwarden_key()
            with self.opener.open(
                urllib.request.Request(
                    "https://gwarden.su/v1/models",
                    headers={"Authorization": "Bearer " + key, "User-Agent": "TravelTech-Skills/1.0"},
                ),
                timeout=deadline.timeout(25),
            ) as response:
                raw = deadline.read(response, 2000000)
            if key.encode() in raw:
                raise Failure("SECRET_ECHO")
            data = strict_json(raw)
            if not isinstance(data, dict) or not isinstance(data.get("data"), list) or not any(
                isinstance(row, dict) and row.get("id") == model for row in data["data"]
            ):
                raise Failure("GWARDEN_MODEL_NOT_ADVERTISED")
            # The upstream promotion is temporary. Do not claim a verified zero price.
            self.prices[p] = (D("0"), D("0"))
            proof = {
                "model_advertised": True,
                "promotional_rate": "UNVERIFIED",
                "basis": "OWNER_CONFIRMED_TEMPORARY_FREE_PROMOTION; NO_PAYMENT_AUTHORIZED",
            }
        elif group == "InkStone":
            key = read_dpapi()
            with self.opener.open(
                urllib.request.Request(
                    "https://discovery-api.intern-ai.org.cn/v1/models",
                    headers={"Authorization": "Bearer " + key},
                ),
                timeout=deadline.timeout(25),
            ) as response:
                raw = deadline.read(response, 2000000)
            if key.encode() in raw:
                raise Failure("SECRET_ECHO")
            if not any(
                x.get("id") == model and x.get("is_ready") is True
                for x in strict_json(raw).get("data", [])
            ):
                raise Failure("INKSTONE_MODEL_NOT_READY")
            with self.opener.open(SOURCE, timeout=deadline.timeout(25)) as response:
                doc = deadline.read(response, 500000)
            expected = (ROOT / "skills/pricing_inkstone_verified.md").read_bytes()
            model_price = verify_model_price(
                expected.decode("utf-8"), doc.decode("utf-8"), model, RATES[model]
            )
            self.prices[p] = tuple(D(str(v)) / 1000000 for v in RATES[model])
            proof = {
                "pricing_source": SOURCE,
                "source_sha256": hashlib.sha256(doc).hexdigest(),
                "model_price": model_price,
                "balance": "UNAVAILABLE_WITH_API_KEY",
                "basis": "EXISTING_USER_PROVIDED_CREDIT; NO_PURCHASE",
            }
        elif p == "Jev":
            rows = self.legacy.get("OneAPI", "/v1/models")["data"]
            row = next(x for x in rows if x["id"] == model)
            ac = self.legacy.get("OneAPI", "/v1/me")
            if ac.get("status") != "active" or D(str(ac["balance_usd"])) != 0:
                raise Failure("EXISTING_FREE_CREDIT_REQUIRED")
            self.prices[p] = (D(row["pricing"]["prompt"]), D(row["pricing"]["completion"]))
            proof = {"free_credit": ac["free_credit_usd"]}
        else:
            self.legacy.preflight(group)
            self.prices[p] = self.legacy.prices[group]
            proof = self.legacy.proofs[group]
        self.proofs[p] = {**proof, "at": time.time()}
        (self.state / (p + "_proof.json")).write_text(canonical(self.proofs[p]), encoding="utf-8")

    def slot(self, scope):
        if self.network:
            return
        with closing(sqlite3.connect(self.rate_path, timeout=60, isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT next FROM slots WHERE scope=?", (scope,)).fetchone()
            slot = max(time.time(), row[0] if row else 0)
            db.execute(
                "INSERT INTO slots VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET next=excluded.next",
                (scope, slot + 1.4),
            )
            db.commit()
        while (delay := slot - time.time()) > 0:
            deadline.sleep(min(delay, 1))

    def call(self, p, messages, tools=None, tag=None, max_tokens=None, extra=None):
        if p not in PROFILES:
            raise Failure("SKILL_ROUTE_UNKNOWN")
        group, model = PROFILES[p]
        if group == "Logicc":
            if extra:
                raise Failure("UNSUPPORTED_PROFILE_OPTION")
            from logicc_transport import LogiccTransport

            delegate = LogiccTransport(self.state / "logicc_native", model=model)
            delegate.check_agent_capacity(p)
            return delegate.call(p, messages, tools=tools, tag=tag, max_tokens=max_tokens)
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or 8192,
            "temperature": 0,
            "stream": False,
        }
        if p == "Gwarden_Gemini4Argon":
            # Gemini 4 Argon ignores temperature; prefer low thinking for
            # the bounded tool-calling cycle and gateway's OpenAI interface.
            payload.pop("temperature", None)
            payload["reasoning_effort"] = "low"
        if tools:
            payload.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        if model in ("intern-s2", "Agents-A1"):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if p == "Pollinations":
            payload["reasoning_effort"] = "low"
        if extra:
            if set(extra) - {"response_format", "tool_choice", "parallel_tool_calls"}:
                raise Failure("UNSUPPORTED_PROFILE_OPTION")
            payload.update(extra)
        return self.dispatch(p, group, model, "/v1/chat/completions", payload, tag)

    def jev(self, state, questions, tag):
        r = self.dispatch(
            "Jev",
            "OneAPI",
            "jev-1.13",
            "/v1/systemone",
            {"model": "jev-1.13", "state": state, "questions": questions},
            tag,
        )
        if r["state"] != "RESPONSE_RECEIVED" or r.get("http") != 200:
            raise Failure(r.get("error") or "JEV_UNAVAILABLE")
        return r["systemone"]

    def dispatch(self, p, group, model, path, payload, tag):
        deadline.check()
        if (self.state / "STOP_INFERENCE").exists():
            raise Failure("STOP_REQUESTED")
        self.purge_payloads()
        check_raw = canonical(payload).encode()
        fingerprint = hashlib.sha256(check_raw).hexdigest()
        tag = tag or uuid.uuid4().hex
        with self.db() as db:
            old = db.execute("SELECT record FROM requests WHERE tag=?", (tag,)).fetchone()
        if old:
            old = strict_json(old[0])
            if old.get("payload_hash") != fingerprint or old.get("provider") != p:
                raise Failure("REQUEST_ID_CONFLICT")
            if old.get("state") in ("RESERVED", "UNKNOWN_OUTCOME"):
                raise Failure("UNKNOWN_NO_RETRY")
            if old.get("response_usable") is False:
                raise Failure(old.get("error", "INFERENCE_ENVELOPE_INVALID"))
            if old.get("payload_state"):
                raise Failure("RESPONSE_BODY_EXPIRED_NO_REPLAY")
            return old
        self.prior_guards(p, model)
        if p not in self.prices or time.time() - self.proofs[p]["at"] > 600:
            self.preflight(p)
        key = (
            "FAKE_TEST_ONLY_KEY"
            if self.network
            else read_dpapi()
            if group == "InkStone"
            else gwarden_key()
            if group == "Gwarden"
            else self.legacy.key(group)
        )
        raw = canonical(payload).encode()
        fingerprint = hashlib.sha256(raw).hexdigest()
        tag = tag or uuid.uuid4().hex
        if key.encode() in raw:
            raise Failure("SECRET_IN_REQUEST")
        if (
            len(raw) > 240000
            or type(payload.get("max_tokens", 8192)) is not int
            or not 1 <= payload.get("max_tokens", 8192) <= 32768
        ):
            raise Failure("REQUEST_BODY_LIMIT")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT record FROM requests WHERE tag=?", (tag,)).fetchone()
            if old:
                db.rollback()
                r = strict_json(old[0])
                if r["payload_hash"] != fingerprint or r["provider"] != p:
                    raise Failure("REQUEST_ID_CONFLICT")
                if r["state"] in ("RESERVED", "UNKNOWN_OUTCOME"):
                    raise Failure("UNKNOWN_NO_RETRY")
                if r.get("response_usable") is False:
                    raise Failure(r.get("error", "INFERENCE_ENVELOPE_INVALID"))
                if r.get("payload_state"):
                    raise Failure("RESPONSE_BODY_EXPIRED_NO_REPLAY")
                return r
            history = [strict_json(row[0]) for row in db.execute("SELECT record FROM requests")]
            fingerprint_of_inference = operation_hash(payload.get("messages"), payload.get("tools"))
            policy = self._iishko_policy(db) if p == "IISHKO" else None
            reason = iishko_reason(history, p, fingerprint_of_inference, policy)
            recovery = read_only_recovery_proof()
            recovered_origin = None
            if reason == "UNKNOWN_OPERATION_NO_REPLAY" and recovery is not None:
                # Explicit exception: one *new* inference, different account,
                # only when the original IISHKO operation is durably UNKNOWN.
                # The evidence is checked INSIDE the reservation transaction.
                origins = [
                    x
                    for x in history
                    if x.get("id") == recovery.get("origin_id")
                    and x.get("tag") == recovery.get("origin_tag")
                    and x.get("provider") == "IISHKO"
                    and x.get("state") == "UNKNOWN_OUTCOME"
                    and x.get("operation_hash") == fingerprint_of_inference
                ]
                if (
                    len(origins) != 1
                    or p != "InkStone_AgentsA1"
                    or recovery.get("target_route") != p
                    or recovery.get("operation_hash") != fingerprint_of_inference
                    or any(
                        x.get("read_only_recovery_of") == recovery.get("origin_id") for x in history
                    )
                ):
                    raise Failure("READONLY_RECOVERY_PROOF_INVALID_OR_ALREADY_USED")
                recovered_origin = origins[0]
                remaining = [x for x in history if x is not recovered_origin]
                reason = blocked_reason(remaining, p, fingerprint_of_inference)
            if reason:
                raise Failure(reason)
            for (record,) in db.execute("SELECT record FROM requests"):
                old = strict_json(record)
                same = old["provider"] == p or (group == "OneAPI" and old.get("group") == "OneAPI")
                if (
                    same
                    and old["state"] in ("RESERVED", "UNKNOWN_OUTCOME")
                    and not (p == "IISHKO" and old.get("id") in accepted_ids(history, policy))
                ):
                    raise Failure("UNKNOWN_NO_RETRY")
                if (same or group == "InkStone" and old.get("group") == "InkStone") and old.get(
                    "http"
                ) in (401, 402, 403):
                    raise Failure("ACCOUNT_AUTH_STOP")
                if same and old.get("retry_at", 0) > time.time():
                    raise Failure("PROVIDER_COOLDOWN")
            # A real free wallet, not a project-imposed cap.
            if not self.network and group == "Pollinations":
                wallet = self.legacy.get(group, "/account/balance")["accountBalance"]
                a, b = self.prices[p]
                reserve = 2 * (a * (len(raw) + 2048) + b * payload.get("max_tokens", 8192))
                if D(str(wallet["paid"])) != 0 or D(str(wallet["tier"])) < reserve:
                    raise Failure("REAL_FREE_BALANCE_INSUFFICIENT")
            if not self.network and group == "OneAPI":
                ac = self.legacy.get(group, "/v1/me")
                a, b = self.prices[p]
                if D(str(ac["balance_usd"])) != 0 or D(str(ac["free_credit_usd"])) < 2 * (
                    a * (len(raw) + 2048) + b * payload.get("max_tokens", 0)
                ):
                    raise Failure("REAL_FREE_BALANCE_INSUFFICIENT")
            deadline.check()
            r = {
                "id": uuid.uuid4().hex,
                "tag": tag,
                "provider": p,
                "group": group,
                "model": model,
                "state": "RESERVED",
                "at": now(),
                "payload_hash": fingerprint,
                "account_scope": scope(p),
                "operation_hash": operation_hash(payload.get("messages"), payload.get("tools")),
                "request": payload,
                "unit": "promotional_rate_unverified"
                if group == "Gwarden"
                else "ink_points"
                if group == "InkStone"
                else "pollen"
                if group == "Pollinations"
                else "provider_usd_estimate",
            }
            if recovered_origin is not None:
                r["read_only_recovery_of"] = recovered_origin["id"]
                r["recovery_policy"] = "EXPLICIT_READONLY_DIFFERENT_ACCOUNT_ONCE"
            db.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?)",
                (r["id"], tag, p, r["state"], canonical(r)),
            )
            db.commit()
        start = time.monotonic()
        dispatched = False
        try:
            deadline.check()
            if self.network:
                dispatched = True
                data = self.network(p, path, payload)
                r["http"] = 200
                r["state"] = "RESPONSE_RECEIVED"
            else:
                self.slot(group)
                host = (
                    "https://discovery-api.intern-ai.org.cn"
                    if group == "InkStone"
                    else "https://gwarden.su"
                    if group == "Gwarden"
                    else ROUTES[group][0]
                )
                req = urllib.request.Request(
                    host + path,
                    data=raw,
                    headers={
                        "Authorization": "Bearer " + key,
                        "Content-Type": "application/json",
                        "User-Agent": "TravelTech-Skills/1.0",
                        "X-Request-ID": r["id"],
                    },
                    method="POST",
                )
                socket_timeout = deadline.timeout(240)
                dispatched = True
                with self.opener.open(req, timeout=socket_timeout) as response:
                    r["http"] = response.status
                    body = deadline.read(response, 4000001)
                    if len(body) > 4000000:
                        raise Failure("RESPONSE_BODY_LIMIT")
                    if key.encode() in body:
                        raise Failure("SECRET_ECHO")
                    r["response_sha256"] = hashlib.sha256(body).hexdigest()
                    r["state"] = "RESPONSE_RECEIVED"
                    data = strict_json(body)
            if key in canonical(data):
                raise Failure("SECRET_ECHO")
            if not isinstance(data, dict):
                raise Failure("INFERENCE_ENVELOPE_INVALID")
            r.update(
                state="RESPONSE_RECEIVED",
                reported_model=data.get("model"),
                remote_id=data.get("id"),
                usage=data.get("usage", {}),
            )
            if p == "Jev":
                r["systemone"] = data
            else:
                r.update(response_record(data, model))
                if group == "Gwarden" and data.get("model") != model:
                    r.update(response_usable=False, error="GWARDEN_MODEL_IDENTITY_UNCONFIRMED")
            u = r["usage"]
            a, b = self.prices[p]
            i = u.get("prompt_tokens", u.get("input_tokens"))
            o = u.get("completion_tokens", u.get("output_tokens"))
            if type(i) is int and type(o) is int and i >= 0 and o >= 0:
                r["estimated_cost"] = (
                    "UNVERIFIED_PROMOTIONAL_RATE" if group == "Gwarden"
                    else str(a * i + b * o)
                )
            else:
                r["usage_unreported"] = True
        except urllib.error.HTTPError as exc:
            r.update(state="HTTP_ERROR", http=exc.code, error="HTTP_" + str(exc.code))
            if exc.code == 429 or exc.code in (500, 502, 503, 504):
                r["retry_at"] = time.time() + retry_seconds(
                    exc.headers.get("Retry-After") if exc.headers else None,
                    default=60 if exc.code == 429 else 30,
                )
        except Exception as exc:
            if r.get("state") == "RESPONSE_RECEIVED":
                r.update(response_usable=False, error="INFERENCE_ENVELOPE_INVALID")
            else:
                r.update(
                    state="UNKNOWN_OUTCOME" if dispatched else "LOCAL_FAILURE",
                    inference_dispatched=dispatched,
                    error=str(exc) if isinstance(exc, Failure) else type(exc).__name__,
                )
        r["latency_sec"] = round(time.monotonic() - start, 3)
        with self.db() as db:
            db.execute(
                "UPDATE requests SET state=?,record=? WHERE id=?",
                (r["state"], canonical(r), r["id"]),
            )
        if r.get("response_usable") is False:
            raise Failure(r.get("error", "INFERENCE_ENVELOPE_INVALID"))
        return r
