"""Bounded InkStone experiments; DPAPI key, durable dispatch ledger, no retries."""

import base64, ctypes, ctypes.wintypes as w, hashlib, json, os, sqlite3
import threading, time, urllib.request, urllib.error, uuid, re
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from provider_transport import Failure, NoRedirect, strict_json, canonical
from trusted_tls import verified_context

RATES = {
    "Agents-A1": (0, 0),
    "intern-s2": (0, 0),
    "Atria-Dawn-Preview": (0, 0),
    "deepseek-v4-flash-0731": (1, 4),
    "deepseek-v4-flash-vision": (1, 4),
    "deepseek-v4-pro-0813": (4.5, 13.5),
    "glm-5.3": (8, 28),
    "kimi-k2.6": (6.5, 27),
    "minimax-m3": (2.1, 8.4),
    "qwen3.8-27b": (0.8, 2.7),
}
SOURCE = "https://cdn-static.openxlab.org.cn/magic-maker/action-static/tokenplan-doc/quick-start/04-model-limits-and-pricing.md"


def now():
    return datetime.now(timezone.utc).isoformat()


def read_dpapi(path=None):
    if os.name != "nt":
        raise Failure("DPAPI_REQUIRES_WINDOWS")
    if path is None:
        secret_dir = Path(os.environ.get("TRAVELTECH_SECRETS_DIR") or Path.home() / ".traveltech-secrets")
        portable_key = secret_dir / "inkstone.key"
        if portable_key.is_file():
            value = portable_key.read_text(encoding="utf-8-sig").strip()
            if not 8 <= len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
                raise Failure("KEY_FORMAT_INVALID")
            return value
    path = Path(path or (Path.home() / ".traveltech-secrets" / "inkstone.key.dpapi"))

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", w.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    data = path.read_bytes()
    if not 1 <= len(data) <= 32768:
        raise Failure("KEY_FILE_SIZE_INVALID")
    buf = ctypes.create_string_buffer(data)
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dst = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData
    fn.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        w.DWORD,
        ctypes.POINTER(Blob),
    ]
    fn.restype = w.BOOL
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise Failure("KEY_UNPROTECT_FAILED")
    try:
        value = ctypes.string_at(dst.pbData, dst.cbData).decode("ascii").strip()
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)
    if not 8 <= len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise Failure("KEY_FORMAT_INVALID")
    return value


class InkstoneTransport:
    _rate_lock = threading.Lock()
    _next = 0.0

    def __init__(
        self,
        state,
        model="Agents-A1",
        network=None,
        allow_unpriced_probe=False,
        stage="lab",
        require_free=False,
        disable_thinking=False,
    ):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.network = network
        self.stage = stage
        self.allow_unpriced_probe = allow_unpriced_probe
        self.require_free = require_free
        self.free_proven = False
        self.disable_thinking = disable_thinking
        self.opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=verified_context())
        )
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,tag TEXT UNIQUE,model TEXT,state TEXT,reserve TEXT,record TEXT)"
            )

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.state / "inkstone.sqlite", timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self):
        with self.db() as db:
            return [
                strict_json(r[0]) for r in db.execute("SELECT record FROM requests ORDER BY rowid")
            ]

    def check_agent_capacity(self, p=None, steps=4):
        rows = self.rows()
        # Model identities are independent explicit selections; there is no automatic fallback.
        if any(
            r["state"] in ("RESERVED", "UNKNOWN_OUTCOME") and r["model"] == self.model for r in rows
        ):
            raise Failure("INKSTONE_UNKNOWN_NO_RETRY")
        if any(r.get("http") in (401, 402, 403, 429) for r in rows):
            raise Failure("INKSTONE_ACCOUNT_CIRCUIT_OPEN")
        if len(rows) + steps > 200:
            raise Failure("INKSTONE_CALL_LIMIT")
        if self.model not in RATES and not self.allow_unpriced_probe:
            raise Failure("INKSTONE_PRICE_UNKNOWN")
        if self.require_free and not self.free_proven:
            self.free_preflight()

    def free_preflight(self):
        if RATES.get(self.model) != (0, 0):
            raise Failure("INKSTONE_FREE_MODEL_REQUIRED")
        if self.network:
            self.free_proven = True
            return
        key = read_dpapi()
        with self.opener.open(
            urllib.request.Request(
                "https://discovery-api.intern-ai.org.cn/v1/models",
                headers={"Authorization": "Bearer " + key},
            ),
            timeout=25,
        ) as response:
            body = response.read(2000000)
        if key.encode() in body:
            raise Failure("SECRET_ECHO")
        models = strict_json(body).get("data", [])
        if not any(x.get("id") == self.model and x.get("is_ready") is True for x in models):
            raise Failure("INKSTONE_MODEL_NOT_READY")
        with self.opener.open(SOURCE, timeout=25) as response:
            doc = response.read(500000).decode("utf-8")
        labels = {
            "Agents-A1": "Agents-A1",
            "intern-s2": "书生-S2",
            "Atria-Dawn-Preview": "Atria-Dawn-Preview",
        }
        row = next(
            (
                line
                for line in doc.splitlines()
                if line.startswith("|")
                and line.split("|")[1].strip() == labels[self.model]
                and len(line.split("|")) >= 7
                and [x.strip() for x in line.split("|")[2:5]] == ["0", "0", "0"]
            ),
            None,
        )
        if row is None:
            raise Failure("INKSTONE_FREE_PRICE_NOT_CONFIRMED")
        proof = {
            "at": now(),
            "model": self.model,
            "source": SOURCE,
            "source_sha256": hashlib.sha256(doc.encode()).hexdigest(),
            "zero_price_row": row,
            "balance_unavailable": True,
            "basis": "PUBLIC_DOCUMENT_NOT_WALLET_DEBIT",
        }
        (self.state / (self.model + "_free_preflight.json")).write_text(
            json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.free_proven = True

    def call(self, p, messages, tools=None, tag=None, max_tokens=None, extra=None):
        if (self.state / "STOP_INFERENCE").exists():
            raise Failure("STOP_REQUESTED")
        if self.require_free and not self.free_proven:
            self.free_preflight()
        key = read_dpapi() if self.network is None else "FAKE_TEST_ONLY_KEY"
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or 2048,
            "stream": False,
            "temperature": 0,
        }
        if self.disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if tools:
            payload.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        if extra:
            payload.update(extra)
        raw = canonical(payload).encode("utf-8")
        if key.encode() in raw:
            raise Failure("SECRET_IN_REQUEST")
        if len(raw) > 90000 or not 1 <= payload["max_tokens"] <= 4096:
            raise Failure("INKSTONE_BODY_LIMIT")
        tag = tag or uuid.uuid4().hex
        rates = self.model in RATES
        a, b = map(lambda x: Decimal(str(x)) / 1000000, RATES.get(self.model, (8, 28)))
        reserve = 2 * (a * (len(raw) + 2048) + b * payload["max_tokens"])
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT record FROM requests WHERE tag=?", (tag,)).fetchone()
            if previous:
                r = strict_json(previous[0])
                if r["payload_hash"] != hashlib.sha256(raw).hexdigest():
                    raise Failure("REQUEST_ID_CONFLICT")
                if r["state"] in ("RESERVED", "UNKNOWN_OUTCOME"):
                    raise Failure("INKSTONE_UNKNOWN_NO_RETRY")
                return r
            rows = [strict_json(x[0]) for x in db.execute("SELECT record FROM requests")]
            if any(
                r["state"] in ("RESERVED", "UNKNOWN_OUTCOME") and r.get("model") == self.model
                for r in rows
            ):
                raise Failure("INKSTONE_UNKNOWN_NO_RETRY")
            if any(r.get("http") in (401, 402, 403, 429) for r in rows):
                raise Failure("INKSTONE_ACCOUNT_CIRCUIT_OPEN")
            if any(
                r.get("http") in (500, 502, 503, 504) and r["model"] == self.model for r in rows
            ):
                raise Failure("INKSTONE_MODEL_CIRCUIT_OPEN")
            if len(rows) >= 200:
                raise Failure("INKSTONE_CALL_LIMIT")
            if not rates and (
                not self.allow_unpriced_probe or any(r["model"] == self.model for r in rows)
            ):
                raise Failure("INKSTONE_PRICE_UNKNOWN")
            if sum(Decimal(r["reserve"]) for r in rows) + reserve > Decimal("3"):
                raise Failure("INKSTONE_BUDGET_LIMIT")
            r = {
                "id": uuid.uuid4().hex,
                "tag": tag,
                "at": now(),
                "provider": p,
                "model": self.model,
                "stage": self.stage,
                "state": "RESERVED",
                "reserve": str(reserve),
                "pricing_verified": rates,
                "payload_hash": hashlib.sha256(raw).hexdigest(),
                "request": payload,
            }
            db.execute(
                "INSERT INTO requests VALUES(?,?,?,?,?,?)",
                (r["id"], tag, self.model, r["state"], str(reserve), canonical(r)),
            )
        start = time.monotonic()
        try:
            if self.network:
                data = self.network(payload)
                headers = {}
                r["http"] = 200
            else:
                with self._rate_lock:
                    delay = max(0, self.__class__._next - time.monotonic())
                    self.__class__._next = max(self.__class__._next, time.monotonic()) + 1.6
                if delay:
                    time.sleep(delay)
                req = urllib.request.Request(
                    "https://discovery-api.intern-ai.org.cn/v1/chat/completions",
                    data=raw,
                    headers={
                        "Authorization": "Bearer " + key,
                        "Content-Type": "application/json",
                        "User-Agent": "TravelTech-InkStone/20261015",
                        "X-Request-ID": r["id"],
                    },
                    method="POST",
                )
                with self.opener.open(req, timeout=90) as response:
                    r["http"] = response.status
                    headers = {
                        k: v
                        for k, v in response.headers.items()
                        if k.lower()
                        in (
                            "x-request-id",
                            "request-id",
                            "x-ratelimit-limit-requests",
                            "x-ratelimit-remaining-requests",
                            "x-ratelimit-limit-tokens",
                            "x-ratelimit-remaining-tokens",
                            "retry-after",
                        )
                    }
                    body = response.read(1000001)
                    if len(body) > 1000000:
                        raise Failure("BODY_LIMIT")
                    if key.encode() in body:
                        raise Failure("SECRET_ECHO")
                    data = strict_json(body)
            if key in canonical(data):
                raise Failure("SECRET_ECHO")
            r.update(
                state="RESPONSE_RECEIVED",
                headers=headers,
                reported_model=data.get("model"),
                remote_id=data.get("id"),
                usage=data.get("usage", {}),
            )
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
            if key in canonical(r):
                raise Failure("SECRET_ECHO")
            u = r["usage"]
            if all(
                type(u.get(k)) is int and u[k] >= 0 for k in ("prompt_tokens", "completion_tokens")
            ):
                r["estimated_ink_points"] = (
                    str(a * u["prompt_tokens"] + b * u["completion_tokens"]) if rates else None
                )
            else:
                r["usage_unreported"] = True
        except urllib.error.HTTPError as e:
            r.update(http=e.code, state="HTTP_ERROR", error="HTTP_" + str(e.code))
            try:
                body = e.read(8192).decode(errors="replace").replace(key, "[REDACTED]")
                obj = strict_json(body)
                r["upstream_error"] = obj.get("error")
                r["remote_id"] = obj.get("request_id")
            except Exception:
                pass
        except Exception as e:
            r.update(
                state="UNKNOWN_OUTCOME",
                error=str(e) if isinstance(e, Failure) else type(e).__name__,
            )
        r["latency_sec"] = round(time.monotonic() - start, 3)
        with self.db() as db:
            db.execute(
                "UPDATE requests SET state=?,record=? WHERE id=?",
                (r["state"], canonical(r), r["id"]),
            )
        return r
