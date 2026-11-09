"""Credit-only bounded HTTPS. Durable reservations and append-only receipts, no retries."""

import json, os, time, uuid, sqlite3, threading, urllib.request, urllib.error, hashlib
from pathlib import Path
from decimal import Decimal as D
from datetime import datetime, timezone
from contextlib import contextmanager
from trusted_tls import verified_context
from secret_reader import read_key

ROUTES = {
    "IISHKO": ("https://api.reformboss.com", "deepseek-v4.1-flash", "reformboss.key"),
    "OneAPI": ("https://api.oneapi.id", "muse-spark-1.3-contributor", "oneapi.key"),
    "Pollinations": ("https://gen.pollinations.ai", "openai/gpt-6-luna", "pollinations.key"),
}


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(o):
    return json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def strict_json(raw):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError("DUPLICATE_KEY")
            out[k] = v
        return out

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("NONFINITE")),
    )


class Failure(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise Failure("REDIRECT_REFUSED")


class CreditTransport:
    def __init__(self, state, stage="integration", network=None):
        if stage not in ("bench", "integration"):
            raise Failure("STAGE_INVALID")
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.stage = stage
        self.lock = threading.RLock()
        import deadline_http

        self.opener = deadline_http.opener(NoRedirect(), context=verified_context())
        self.network = network
        self.prices = {}
        self.limits = {}
        self.proofs = {}
        self.blocked = {}
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,provider TEXT,stage TEXT,tag TEXT,state TEXT,reserve TEXT,record TEXT,UNIQUE(provider,stage,tag))"
            )

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.state / "credit.sqlite", timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def key(self, p):
        keydir = Path(os.environ.get("TRAVELTECH_SECRETS_DIR") or Path.home() / ".traveltech-secrets")
        return read_key(keydir / ROUTES[p][2])

    def get(self, p, path, auth=True):
        import request_deadline as deadline

        deadline.check()
        allowed = {
            "IISHKO": (
                "/api/pricing",
                "/v1/dashboard/billing/subscription",
                "/v1/dashboard/billing/usage",
            ),
            "OneAPI": ("/v1/me", "/v1/models"),
            "Pollinations": (
                "/account/balance",
                "/account/key",
                "/account/key/usage",
                "/text/models",
            ),
        }
        if p not in allowed or path not in allowed[p]:
            raise Failure("HOST_OR_PATH_REFUSED")
        key = self.key(p) if auth else None
        if self.network:
            return self.network(p, path, None)
        h = {"User-Agent": "TravelTech-credit-only/20261015"}
        if key:
            h["Authorization"] = "Bearer " + key
        with self.opener.open(
            urllib.request.Request(ROUTES[p][0] + path, headers=h), timeout=deadline.timeout(25)
        ) as r:
            raw = deadline.read(r, 4000000)
            if key and key.encode() in raw:
                raise Failure("SECRET_ECHO")
            return strict_json(raw)

    def preflight(self, p):
        guard = Path(__file__).resolve().parents[1] / "ROUTE_GUARDS.json"
        if guard.exists() and p in json.loads(guard.read_text(encoding="utf-8")).get("blocked", {}):
            raise Failure("UNRESOLVED_PRIOR_OUTCOME_ROUTE_BLOCKED")
        if p == "IISHKO":
            d = self.get(p, "/api/pricing", False)
            m = next(x for x in d["data"] if x["model_name"] == ROUTES[p][1])
            if m["quota_type"] != 0 or m.get("billing_mode"):
                raise Failure("UNKNOWN_PRICING")
            a = (
                D(str(m["model_ratio"]))
                * 2
                * max(D(str(x)) for x in d["group_ratio"].values())
                / 1000000
            )
            b = a * D(str(m["completion_ratio"]))
            account = self.get(p, "/v1/dashboard/billing/subscription")
            proof = {k: account.get(k) for k in ("hard_limit_usd", "soft_limit_usd")}
            if D(str(proof["hard_limit_usd"])) <= 2:
                raise Failure("EXISTING_GRANT_INSUFFICIENT")
            limit = D("1.5") if self.stage == "bench" else D(".25")
            proof["basis"] = (
                "USER_IDENTIFIED_EXISTING_GRANT; ENDPOINT_UNITS_NOT_INDEPENDENTLY_PROVEN"
            )
        elif p == "OneAPI":
            account = self.get(p, "/v1/me")
            m = next(x for x in self.get(p, "/v1/models")["data"] if x["id"] == ROUTES[p][1])
            a = D(m["pricing"]["prompt"])
            b = D(m["pricing"]["completion"])
            if account.get("status") != "active" or D(account["balance_usd"]) != 0:
                raise Failure("FREE_ONLY_ACCOUNT_REQUIRED")
            free = D(account["free_credit_usd"])
            limit = min(D(".5") if self.stage == "bench" else D(".15"), free)
            if limit <= 0:
                raise Failure("FREE_CREDIT_EMPTY")
            proof = {k: account.get(k) for k in ("status", "balance_usd", "free_credit_usd")}
        else:
            account = self.get(p, "/account/balance")
            keyinfo = self.get(p, "/account/key")
            m = next(x for x in self.get(p, "/text/models", False) if x["name"] == ROUTES[p][1])
            wallet = account["accountBalance"]
            free = D(str(wallet["tier"]))
            if (
                keyinfo.get("valid") is not True
                or D(str(wallet["paid"])) != 0
                or m.get("paid_only")
                or m.get("community")
                or m["pricing"]["currency"] != "pollen"
            ):
                raise Failure("QUEST_ONLY_REQUIRED")
            a = D(m["pricing"]["promptTextTokens"])
            b = D(m["pricing"]["completionTextTokens"])
            limit = (
                min(D(".16"), free * D(".7"), free - D(".05"))
                if self.stage == "bench"
                else min(D(".10"), free - D(".05"))
            )
            if limit <= 0:
                raise Failure("RESERVE_BALANCE_TOO_LOW")
            proof = {"wallet": wallet, "model_paid_only": m.get("paid_only"), "key_valid": True}
        if not all(x.is_finite() and 0 <= x <= D(".000012") for x in (a, b)):
            raise Failure("PRICE_UNVERIFIED")
        # Preserve the first stage limit across restarts; never reset allowance by rerunning.
        path = self.state / (self.stage + "_" + p + "_preflight.json")
        if path.exists():
            limit = min(limit, D(json.loads(path.read_text())["limit"]))
        self.prices[p] = (a, b)
        self.limits[p] = limit
        self.proofs[p] = proof
        path.write_text(
            json.dumps(
                {
                    "at": now(),
                    "proof": proof,
                    "limit": str(limit),
                    "input": str(a),
                    "output": str(b),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return proof

    def rows(self):
        with self.db() as db:
            return [json.loads(x[0]) for x in db.execute("SELECT record FROM calls ORDER BY rowid")]

    def check_agent_capacity(self, p, steps=4):
        if p not in self.prices:
            self.preflight(p)
        rows = [r for r in self.rows() if r["provider"] == p]
        if any(r["state"] in ("RESERVED", "UNKNOWN_OUTCOME") for r in rows):
            raise Failure("UNKNOWN_ROUTE_BLOCKED")
        own = [r for r in rows if r["stage"] == self.stage]
        if len(own) + steps > (60 if self.stage == "bench" else 20):
            raise Failure("INSUFFICIENT_CALL_ALLOWANCE_FOR_COMPLETE_CYCLE")

    def append(self, event):
        with (self.state / "credit_receipts.jsonl").open("a", encoding="utf-8") as f:
            f.write(canonical(event) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def call(self, p, messages, tools=None, tag=None, max_tokens=None):
        if p not in ROUTES:
            raise Failure("PROVIDER_INVALID")
        if (self.state / "STOP_INFERENCE").exists():
            raise Failure("STOP_REQUESTED")
        if p not in self.prices:
            self.preflight(p)
        key = self.key(p) if not self.network else "FAKE_TEST_CREDENTIAL_0000000000"
        keys = [self.key(q) for q in ROUTES] if not self.network else [key]
        payload = {
            "model": ROUTES[p][1],
            "messages": messages,
            "max_tokens": max_tokens or (4096 if p == "OneAPI" else 900),
            "stream": False,
        }
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if p in ("OneAPI", "Pollinations"):
            payload["reasoning_effort"] = "low"
        raw = canonical(payload).encode()
        if any(k.encode() in raw for k in keys):
            raise Failure("SECRET_IN_REQUEST")
        if len(raw) > 90000:
            raise Failure("INPUT_TOO_LARGE")
        a, b = self.prices[p]
        input_cap = len(raw) + 2048
        reserve = 2 * (a * input_cap + b * payload["max_tokens"])
        tag = tag or uuid.uuid4().hex
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT record FROM calls WHERE provider=? AND stage=? AND tag=?",
                (p, self.stage, tag),
            ).fetchone()
            if existing:
                r = json.loads(existing[0])
                if r["payload_hash"] != hashlib.sha256(raw).hexdigest():
                    raise Failure("REQUEST_ID_CONFLICT")
                if r["state"] in ("RESERVED", "UNKNOWN_OUTCOME"):
                    raise Failure("UNKNOWN_NO_RETRY")
                return r
            rows = [
                json.loads(x[0])
                for x in db.execute("SELECT record FROM calls WHERE provider=?", (p,))
            ]
            if any(r["state"] in ("RESERVED", "UNKNOWN_OUTCOME") for r in rows):
                raise Failure("UNKNOWN_ROUTE_BLOCKED")
            if any(r.get("http") in (401, 402, 403, 429, 500, 502, 503, 504) for r in rows):
                raise Failure("CIRCUIT_OPEN")
            own = [r for r in rows if r["stage"] == self.stage]
            if len(own) >= (60 if self.stage == "bench" else 20):
                raise Failure("CALL_LIMIT")
            if sum(D(r["reserve"]) for r in own) + reserve > self.limits[p]:
                raise Failure("BUDGET_LIMIT")
            # A fresh balance protects against other clients consuming the same free wallet.
            if p == "Pollinations":
                wallet = self.get(p, "/account/balance")["accountBalance"]
                if D(str(wallet["paid"])) != 0 or D(str(wallet["tier"])) - reserve < D(".05"):
                    raise Failure("QUEST_RESERVE_LIMIT")
            if p == "OneAPI":
                ac = self.get(p, "/v1/me")
                if D(ac["balance_usd"]) != 0 or D(ac["free_credit_usd"]) < reserve:
                    raise Failure("FREE_CREDIT_INSUFFICIENT")
            r = {
                "id": uuid.uuid4().hex,
                "at": now(),
                "provider": p,
                "model": ROUTES[p][1],
                "stage": self.stage,
                "tag": tag,
                "payload_hash": hashlib.sha256(raw).hexdigest(),
                "request": payload,
                "state": "RESERVED",
                "reserve": str(reserve),
                "input_cap": input_cap,
            }
            db.execute(
                "INSERT INTO calls VALUES(?,?,?,?,?,?,?)",
                (r["id"], p, self.stage, tag, "RESERVED", str(reserve), canonical(r)),
            )
            self.append({"event": "RESERVED", **r})
        start = time.monotonic()
        dispatched = False
        try:
            dispatched = True
            if self.network:
                data = self.network(p, "/v1/chat/completions", payload)
            else:
                req = urllib.request.Request(
                    ROUTES[p][0] + "/v1/chat/completions",
                    data=raw,
                    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                    method="POST",
                )
                with self.opener.open(req, timeout=60) as response:
                    r["http"] = response.status
                    body = response.read(1000001)
                    if len(body) > 1000000:
                        raise Failure("BODY_LIMIT")
                    if any(k.encode() in body for k in keys):
                        raise Failure("SECRET_ECHO")
                    data = strict_json(body)
            r["http"] = 200
            r["state"] = "RESPONSE_RECEIVED"
            r["reported_model"] = data.get("model")
            r["remote_id"] = data.get("id")
            r["usage"] = data.get("usage", {})
            r["choices"] = [
                {
                    "finish_reason": c.get("finish_reason"),
                    "message": {
                        k: v
                        for k, v in c.get("message", {}).items()
                        if k in ("role", "content", "tool_calls", "refusal")
                    },
                }
                for c in data.get("choices", [])
            ]
            u = r["usage"]
            if not all(
                type(u.get(k)) is int and u[k] >= 0 for k in ("prompt_tokens", "completion_tokens")
            ):
                raise Failure("USAGE_UNKNOWN")
            r["estimated_cost"] = str(a * u["prompt_tokens"] + b * u["completion_tokens"])
            if u["prompt_tokens"] > input_cap or u["completion_tokens"] > payload["max_tokens"]:
                raise Failure("USAGE_CAP_VIOLATION")
        except urllib.error.HTTPError as e:
            r.update(http=e.code, state="HTTP_ERROR", error="HTTP_" + str(e.code))
            r["retry_after"] = e.headers.get("Retry-After")
        except Exception as e:
            r.update(
                state="UNKNOWN_OUTCOME"
                if r["state"] == "RESERVED" or str(e) in ("USAGE_UNKNOWN", "USAGE_CAP_VIOLATION")
                else "INVALID_RESPONSE",
                error=str(e) if isinstance(e, Failure) else type(e).__name__,
            )
        r["latency_sec"] = round(time.monotonic() - start, 3)
        with self.lock, self.db() as db:
            db.execute(
                "UPDATE calls SET state=?,record=? WHERE id=?", (r["state"], canonical(r), r["id"])
            )
            self.append({"event": "RESULT", **r})
        return r
