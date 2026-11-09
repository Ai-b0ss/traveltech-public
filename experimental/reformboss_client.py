"""Bounded OpenAI-compatible transport. Credentials stay in memory only."""

from pathlib import Path
import json, urllib.request, urllib.error, ssl, socket, sqlite3, uuid, hashlib, time
from datetime import datetime, timezone
from contextlib import contextmanager
from secret_reader import read_key
from trusted_tls import verified_context

BASE_URL = "https://api.reformboss.com/v1"


class ModelError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class TrialBudget:
    def __init__(self, path, limit=3, grant="USER_FREE_TRIAL_MAX_3_X_512", prior_ids=()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        self.grant = grant
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, status TEXT, model TEXT, started TEXT, input_sha256 TEXT, usage TEXT)"
            )
            if "error_code" not in {r[1] for r in db.execute("PRAGMA table_info(attempts)")}:
                db.execute("ALTER TABLE attempts ADD COLUMN error_code TEXT")
            db.execute(
                "CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, total_limit INTEGER)"
            )
            existing = {r[0] for r in db.execute("SELECT id FROM attempts")}
            if not set(prior_ids) <= existing:
                raise ModelError("MODEL_PRIOR_LEDGER_MISSING")
            saved = db.execute("SELECT total_limit FROM grants WHERE id=?", (grant,)).fetchone()
            if saved and saved[0] != limit:
                raise ModelError("MODEL_GRANT_LIMIT_CHANGED")
            db.execute("INSERT OR IGNORE INTO grants VALUES (?,?)", (grant, limit))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        try:
            with db:
                yield db
        finally:
            db.close()

    def status(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,status,model,started,input_sha256,usage,error_code FROM attempts ORDER BY started"
            ).fetchall()
        return {
            "attempts": len(rows),
            "remaining": max(0, self.limit - len(rows)),
            "total_limit": self.limit,
            "authorization": self.grant,
            "blocked_unknown": any(r[1] in ("RESERVED", "UNKNOWN_OUTCOME") for r in rows),
            "records": [
                {
                    "id": r[0],
                    "status": r[1],
                    "model": r[2],
                    "started_at": r[3],
                    "input_sha256": r[4],
                    "usage": json.loads(r[5]) if r[5] else None,
                    "error_code": r[6],
                }
                for r in rows
            ],
        }

    def reserve(self, model, payload):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT status FROM attempts").fetchall()
            if any(r[0] in ("RESERVED", "UNKNOWN_OUTCOME") for r in rows):
                raise ModelError("MODEL_PREVIOUS_OUTCOME_UNKNOWN")
            if len(rows) >= self.limit:
                raise ModelError("MODEL_TRIAL_LIMIT_REACHED")
            identifier = uuid.uuid4().hex
            db.execute(
                "INSERT INTO attempts (id,status,model,started,input_sha256,usage) VALUES (?,?,?,?,?,?)",
                (
                    identifier,
                    "RESERVED",
                    model,
                    datetime.now(timezone.utc).isoformat(),
                    hashlib.sha256(payload).hexdigest(),
                    None,
                ),
            )
            return identifier

    def finish(self, identifier, status, usage=None, error_code=None):
        with self.connect() as db:
            db.execute(
                "UPDATE attempts SET status=?,usage=?,error_code=? WHERE id=?",
                (status, json.dumps(usage) if usage else None, error_code, identifier),
            )


class ReformbossClient:
    def __init__(self, config, budget_path, transport=None, key_path=None):
        self.config = Path(config)
        self.transport = transport
        self.key_path = key_path
        settings = self.settings()
        grant = settings.get("authorization")
        if grant == "USER_GLM_ADDITIONAL_5_X_512_20261013":
            prior = settings.get("prior_attempt_ids", [])
            if len(prior) != 3 or len(set(prior)) != 3 or settings.get("model") != "glm-5.3":
                raise ModelError("MODEL_GRANT_INVALID")
            self.budget = TrialBudget(budget_path, 8, grant, prior)
        elif grant == "USER_REFORMBOSS_NEW_3_X_512_20261013":
            self.budget = TrialBudget(budget_path, 3, grant)
        else:
            self.budget = TrialBudget(budget_path)

    def settings(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def status(self):
        settings = self.settings()
        status = self.budget.status()
        code = None
        try:
            read_key(self.key_path)
        except FileNotFoundError:
            code = "KEY_FILE_NOT_FOUND"
        except (ValueError, UnicodeError):
            code = "KEY_FILE_INVALID"
        if not settings.get("model"):
            code = code or "MODEL_NOT_SELECTED"
        elif settings["model"] not in settings.get("available_model_ids", []):
            code = code or "MODEL_NOT_VERIFIED_AVAILABLE"
        if status["blocked_unknown"]:
            code = "MODEL_PREVIOUS_OUTCOME_UNKNOWN"
        elif not status["remaining"]:
            code = "MODEL_TRIAL_LIMIT_REACHED"
        last_error = next(
            (r["error_code"] for r in reversed(status["records"]) if r["error_code"]), None
        )
        return {
            "model": settings.get("model"),
            "available": code is None,
            "reason": code,
            "last_error": last_error,
            "trial": status,
            "max_output_tokens": 512,
            "additional_limit": 5 if self.budget.limit == 8 else 3,
            "allowance": "USER_AUTHORIZED_EXISTING_BALANCE; NO PURCHASE_ENDPOINTS",
            "current_supplier_calls": 0,
        }

    def _request(self, path, method, body=None, key=None):
        if path not in ("/models", "/chat/completions"):
            raise ModelError("MODEL_ENDPOINT_NOT_ALLOWED")
        key = key or read_key(self.key_path)
        if self.transport:
            return self.transport(path, method, body, key)
        opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=verified_context())
        )
        request = urllib.request.Request(
            BASE_URL + path,
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "TravelTech-local-integration/1.0",
            },
        )
        with opener.open(request, timeout=30) as response:
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ModelError("MODEL_RESPONSE_TOO_LARGE")
            if key.encode() in raw:
                raise ModelError("MODEL_CREDENTIAL_ECHO_BLOCKED")
            return raw

    def discover(self):
        try:
            raw = self._request("/models", "GET")
            data = json.loads(raw)
        except urllib.error.HTTPError as e:
            raise ModelError("MODEL_DISCOVERY_HTTP_" + str(e.code)) from None
        except (TimeoutError, urllib.error.URLError, ssl.SSLError):
            raise ModelError("MODEL_DISCOVERY_TRANSPORT_UNAVAILABLE") from None
        except (ValueError, KeyError, TypeError):
            raise ModelError("MODEL_DISCOVERY_FORMAT_INVALID") from None
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ModelError("MODEL_DISCOVERY_FORMAT_INVALID")
        ids = [
            x["id"]
            for x in data["data"]
            if isinstance(x, dict) and isinstance(x.get("id"), str) and len(x["id"]) <= 160
        ]
        settings = self.settings()
        selected = settings.get("model")
        found = selected in ids
        settings.update(
            available_model_ids=[selected] if found else [],
            observed_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        self.config.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        if not found:
            raise ModelError("CONFIGURED_MODEL_NOT_LISTED")
        return {
            "selected": selected,
            "configured_model_listed": True,
            "models_count": len(ids),
            "source": "AUTHENTICATED_MODELS_GET",
            "billing_metadata_reported": False,
            "fallback": False,
        }

    def infer(self, messages):
        settings = self.settings()
        model = settings.get("model")
        if not model or model not in settings.get("available_model_ids", []):
            raise ModelError("MODEL_NOT_VERIFIED_AVAILABLE")
        if settings.get("authorization") not in (
            "USER_FREE_TRIAL_MAX_3_X_512",
            "USER_GLM_ADDITIONAL_5_X_512_20261013",
            "USER_REFORMBOSS_NEW_3_X_512_20261013",
        ):
            raise ModelError("MODEL_TRIAL_NOT_AUTHORIZED")
        try:
            key = read_key(self.key_path)
        except FileNotFoundError:
            raise ModelError("KEY_FILE_NOT_FOUND") from None
        except (ValueError, UnicodeError):
            raise ModelError("KEY_FILE_INVALID") from None
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": 512,
            "temperature": 0,
            "stream": False,
        }
        if settings.get("response_format", {"type": "json_object"}) is not None:
            body["response_format"] = settings.get("response_format", {"type": "json_object"})
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if key.encode() in payload:
            raise ModelError("MODEL_SECRET_IN_INPUT_BLOCKED")
        identifier = self.budget.reserve(model, payload)
        start = time.perf_counter()
        try:
            raw = self._request("/chat/completions", "POST", payload, key)
            if key.encode() in raw:
                raise ModelError("MODEL_CREDENTIAL_ECHO_BLOCKED")
            data = json.loads(raw)
            choices = data.get("choices", [])
            if not choices or choices[0].get("finish_reason") != "stop":
                raise ModelError("MODEL_RESPONSE_INCOMPLETE")
            content = choices[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ModelError("MODEL_CONTENT_MISSING")
            if key in content:
                raise ModelError("MODEL_CREDENTIAL_ECHO_BLOCKED")
            usage = data.get("usage", {})
            usage = {
                k: v
                for k, v in usage.items()
                if k in ("prompt_tokens", "completion_tokens", "total_tokens")
                and type(v) is int
                and v >= 0
            }
            if usage.get("completion_tokens", 0) > 512:
                raise ModelError("MODEL_OUTPUT_LIMIT_VIOLATION")
            self.budget.finish(identifier, "RESPONSE_RECEIVED", usage)
            return content, {
                "source": "TEST_TRANSPORT" if self.transport else "REAL_MODEL",
                "model": model,
                "attempt_id": identifier,
                "usage": usage,
                "duration_seconds": time.perf_counter() - start,
                "max_output_tokens": 512,
                "automatic_retries": 0,
                "monetary_cost": "NOT_REPORTED_BY_SERVICE",
            }
        except urllib.error.HTTPError as e:
            status = "UNKNOWN_OUTCOME" if e.code >= 500 else "HTTP_ERROR"
            self.budget.finish(identifier, status, error_code="MODEL_HTTP_" + str(e.code))
            raise ModelError("MODEL_HTTP_" + str(e.code)) from None
        except (TimeoutError, socket.timeout, urllib.error.URLError, ssl.SSLError):
            self.budget.finish(
                identifier, "UNKNOWN_OUTCOME", error_code="MODEL_TRANSPORT_OUTCOME_UNKNOWN_NO_RETRY"
            )
            raise ModelError("MODEL_TRANSPORT_OUTCOME_UNKNOWN_NO_RETRY") from None
        except Exception as e:
            self.budget.finish(
                identifier,
                "RESPONSE_REJECTED",
                error_code=str(e) if isinstance(e, ModelError) else "MODEL_RESPONSE_FORMAT_INVALID",
            )
            if isinstance(e, ModelError):
                raise
            raise ModelError("MODEL_RESPONSE_FORMAT_INVALID") from None
