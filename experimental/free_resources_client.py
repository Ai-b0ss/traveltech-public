"""Opt-in free-resource TravelTech gateway: keyless -> bonus free credit -> local.

Reformboss is NEVER imported, called, or configured. No card/payment APIs.
All live calls restricted to a single exact synthetic hotel request per installation.
"""

from __future__ import annotations
import sys, json, sqlite3, threading, hashlib, datetime, time, os, urllib.request, urllib.error
from pathlib import Path
from decimal import Decimal
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))
from freellmpool.config import load_catalog
from freellmpool.router import Pool
from freellmpool.quota import QuotaStore
from freellmpool.client import default_post
from ai_router import AdapterError, ChatAdapter
from natural_language import validate_intent, local_rules
from oneapi_validated_router import safe_semantic_normalization
from app import request_from
from gateway_transport import GatewayTransport
from secret_reader import read_key
from trusted_tls import verified_context

SYNTHETIC_TEXT = "Подбери отель в Сочи с 6 по 9 ноября 2026 года для 2 взрослых в 1 номере, бюджет до 34 тысяч рублей. Нужен завтрак."
KEYLESS_PROVIDER = "pollinations"
KEYLESS_MODEL = "openai-fast"
KEYLESS_HOST = "text.pollinations.ai"
GLM_MODEL = "glm-5.3-flash"
GLM_ROUTE = "OneAPI/" + GLM_MODEL
GLM_ENDPOINT = "https://api.oneapi.id/v1/chat/completions"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def bonus_proof():
    """Fresh authoritative read-only OneAPI balance and model price."""
    key = read_key(Path.home() / ".traveltech-secrets" / "oneapi.key")
    op = urllib.request.build_opener(
        NoRedirect(), urllib.request.HTTPSHandler(context=verified_context())
    )

    def get(route):
        req = urllib.request.Request(
            "https://api.oneapi.id/v1/" + route,
            headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
            method="GET",
        )
        with op.open(req, timeout=12) as response:
            if response.status != 200:
                raise AdapterError("BONUS_PREFLIGHT_NON_200")
            raw = response.read(500000)
        if key.encode() in raw:
            raise AdapterError("SECRET_ECHO_FROM_PREFLIGHT")
        return json.loads(raw)

    account = get("me")
    if account.get("status") != "active" or Decimal(str(account.get("balance_usd", "-1"))) != 0:
        raise AdapterError("NONZERO_PAID_BALANCE_OR_INACTIVE_ACCOUNT")
    free = Decimal(str(account.get("free_credit_usd", 0)))
    if free < Decimal(".004"):
        raise AdapterError("FREE_CREDIT_INSUFFICIENT")
    models = get("models").get("data", [])
    item = next((x for x in models if x.get("id") == GLM_MODEL), None)
    if item is None:
        raise AdapterError("GLM_NOT_AVAILABLE")
    rates = item.get("pricing", {})
    if (
        Decimal(str(rates.get("prompt", "-1"))) < 0
        or Decimal(str(rates.get("completion", "-1"))) < 0
    ):
        raise AdapterError("PRICE_NOT_VERIFIED")
    return {
        "free_credit_usd": str(free),
        "observed_unix": time.time(),
        "input": str(rates["prompt"]),
        "output": str(rates["completion"]),
    }


class FreeResourcesClient:
    """Use only exact synthetic text and a durable per-installation one-attempt ledger."""

    def __init__(self, ledger, allow_network=True, **inject):
        self.path = Path(ledger)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS calls(provider TEXT PRIMARY KEY,status TEXT,reason TEXT)"
        )
        self.lock = threading.RLock()
        self.allow_network = allow_network
        self.keyless_fake = inject.get("keyless_fake")
        self.bonus_fake = inject.get("bonus_fake")
        self.history = []
        self.oneapi_transport = None
        self.keyless_post_count = 0
        self.vendor_catalog = {
            p.id: p for p in load_catalog(ROOT / "vendor/freellmpool/providers.toml")
        }
        public = self.vendor_catalog.get(KEYLESS_PROVIDER)
        if (
            public is None
            or not public.keyless
            or urlsplit(public.base_url).hostname != KEYLESS_HOST
        ):
            raise AdapterError("KEYLESS_CATALOG_INTEGRITY_FAILED")

    def close(self):
        if self.oneapi_transport:
            self.oneapi_transport.close()
        self.db.close()

    def _attempt(self, provider, fn):
        with self.lock:
            if self.db.execute(
                "SELECT COUNT(*) FROM calls WHERE provider=?", (provider,)
            ).fetchone()[0]:
                raise AdapterError("ROUTE_ALREADY_ATTEMPTED_NO_REPEAT")
            self.db.execute(
                "INSERT INTO calls VALUES(?,?,?)",
                (provider, "UNKNOWN_OUTCOME", "RESERVED_BEFORE_DISPATCH"),
            )
        try:
            answer = fn()
            self.db.execute(
                "UPDATE calls SET status=?,reason=? WHERE provider=?",
                ("RESPONSE_RECEIVED", "", provider),
            )
            return answer
        except Exception as exc:
            why = str(exc)[:140]
            # A confirmed provider 402/404 is a definitive rejection, not a billable unknown.
            state = (
                "REJECTED_402_OR_404"
                if ("HTTP 402" in why or "HTTP 404" in why)
                else "UNKNOWN_OR_REJECTED"
            )
            self.db.execute(
                "UPDATE calls SET status=?,reason=? WHERE provider=?",
                (state, type(exc).__name__ + ":" + why, provider),
            )
            raise

    def _pollinations(self, messages):
        if self.keyless_fake is not None:
            return self.keyless_fake(messages)
        if not self.allow_network:
            raise AdapterError("NO_PUBLIC_NETWORK")
        p = self.vendor_catalog[KEYLESS_PROVIDER]
        only = []

        def post(url, headers, body, timeout, **kwargs):
            if urlsplit(url).hostname != KEYLESS_HOST or body.get("model") != KEYLESS_MODEL:
                raise AdapterError("KEYLESS_TARGET_NOT_ALLOWLISTED")
            if len(only) >= 1:
                raise AdapterError("KEYLESS_REPEAT_FORBIDDEN")
            only.append(True)
            self.keyless_post_count += 1
            return default_post(url, headers, body, timeout, max_attempts=1)

        pool = Pool(
            [p],
            env={"FREELLMPOOL_CONFIG_FILE": str(ROOT / "disabled_keys.toml")},
            post=post,
            quota=QuotaStore(ROOT / "experimental/runtime/keyless_quota.json"),
        )
        reply = pool.chat(
            messages,
            model=KEYLESS_MODEL,
            providers=[KEYLESS_PROVIDER],
            timeout=18,
            max_tokens=750,
            temperature=0.0,
        )
        if reply.provider_id != KEYLESS_PROVIDER or reply.model != KEYLESS_MODEL:
            raise AdapterError("KEYLESS_PROVIDER_MODEL_MISMATCH")
        return reply.text

    def _bonus(self, messages):
        if self.bonus_fake is not None:
            return self.bonus_fake(messages)
        if not self.allow_network:
            raise AdapterError("NO_BONUS_NETWORK")
        proof = bonus_proof()
        grant = {
            "mode": "LIVE_TEST",
            "owner_authorized": True,
            "synthetic_only": True,
            "total_limit_usd": ".003",
            "max_total_requests": 1,
            "free_credit_proofs": {
                GLM_ROUTE: {
                    "provider": "OneAPI",
                    "model": GLM_MODEL,
                    "covered_by_free_credit": True,
                    "observed_unix": proof["observed_unix"],
                    "free_credit_usd": proof["free_credit_usd"],
                }
            },
            "pricing_per_token": {GLM_ROUTE: {"input": proof["input"], "output": proof["output"]}},
        }
        if self.oneapi_transport is None:
            self.oneapi_transport = GatewayTransport(
                grant, ROOT / "experimental/runtime/bonus_oneapi_calls.sqlite", timeout_seconds=80
            )
        else:
            self.oneapi_transport.grant = grant
        return ChatAdapter("OneAPI", GLM_MODEL, self.oneapi_transport).infer(messages)

    def status(self):
        entries = self.db.execute("SELECT provider,status FROM calls ORDER BY rowid").fetchall()
        accepted = any(status == "VALIDATED_ACCEPTED" for _, status in entries)
        return {
            "mode": "FREE_RESOURCE_LAB",
            "real_enabled": self.allow_network,
            "routes": [
                {
                    "provider": KEYLESS_PROVIDER,
                    "model": KEYLESS_MODEL,
                    "billing": "KEYLESS_NO_CARD",
                },
                {"provider": "OneAPI", "model": GLM_MODEL, "billing": "EXISTING_FREE_CREDIT_ONLY"},
                {"provider": "LOCAL", "model": None, "billing": "ZERO_NETWORK"},
            ],
            "attempt_count": len(entries),
            "remaining_requests": 0 if accepted else max(0, 2 - len(entries)),
            "last_transport_status": entries[-1][1] if entries else "NOT_ATTEMPTED",
            "automatic_retries": 0,
            "reformboss_enabled": False,
            "trace": self.history,
        }

    def infer(self, messages):
        try:
            data = json.loads(messages[-1]["content"])
            utterance = data["user_text"]
            context = request_from(data["current_request"]) if data.get("current_request") else None
        except (ValueError, KeyError, TypeError):
            raise AdapterError("INVALID_TRAVEL_REQUEST_CONTEXT") from None
        if utterance != SYNTHETIC_TEXT or context is not None:
            raise AdapterError("ONLY_SYNTHETIC_REQUEST_AUTHORIZED")
        trace = []
        if self.db.execute("SELECT 1 FROM calls WHERE status='VALIDATED_ACCEPTED'").fetchone():
            raise AdapterError("SUCCESSFUL_MODEL_ROUTE_ALREADY_USED_NO_REPEAT")

        def checked(provider, model, answer):
            # Never trust a provider's unsupported hotel facts or divergent user parameters.
            proposed = json.loads(answer) if isinstance(answer, str) else answer
            # Public free models often omit country while accurately naming
            # a city. Only fill a deterministic canonical country already
            # grounded in the exact allowlisted user text, never a guess.
            if (
                isinstance(proposed, dict)
                and proposed.get("intent") == "search"
                and isinstance(proposed.get("values"), dict)
                and proposed["values"].get("city") in ("Сочи", "Sochi")
                and "country" not in proposed["values"]
                and "в Сочи" in utterance
            ):
                proposed = {**proposed, "values": {**proposed["values"], "country": "RU"}}
            validated, _, _ = validate_intent(proposed, utterance, None)
            if (
                proposed != validated
                and validated["intent"] not in ("inspect", "clarify")
                and not safe_semantic_normalization(proposed, validated)
            ):
                raise AdapterError("MODEL_SEMANTICALLY_DIVERGENT")
            trace.append({"provider": provider, "model": model, "status": "ACCEPTED"})
            with self.lock:
                self.db.execute(
                    "UPDATE calls SET status='VALIDATED_ACCEPTED',reason='' WHERE provider=?",
                    (provider,),
                )
            self.history = list(trace)
            is_fixture = (
                (self.keyless_fake is not None)
                if provider == KEYLESS_PROVIDER
                else (self.bonus_fake is not None)
            )
            live_count = self.keyless_post_count + (
                len(self.oneapi_transport.attempts()) if self.oneapi_transport else 0
            )
            return json.dumps(validated, ensure_ascii=False), {
                "source": "LOCAL_ADAPTER_FIXTURE"
                if is_fixture
                else ("REAL_KEYLESS" if provider == KEYLESS_PROVIDER else "REAL_BONUS_FREE_CREDIT"),
                "provider": provider,
                "model": model,
                "model_calls": live_count,
                "free_credit_only": provider == "OneAPI",
                "reformboss_calls": 0,
                "automatic_retries": 0,
                "attempts": trace,
                "validated_by": "TRAVEL_DETERMINISTIC_GATE",
            }

        if not self.db.execute(
            "SELECT 1 FROM calls WHERE provider=?", (KEYLESS_PROVIDER,)
        ).fetchone():
            try:
                answer = self._attempt(KEYLESS_PROVIDER, lambda: self._pollinations(messages))
                return checked(KEYLESS_PROVIDER, KEYLESS_MODEL, answer)
            except Exception as e:
                reason = str(e)[:120]
                trace.append(
                    {
                        "provider": KEYLESS_PROVIDER,
                        "model": KEYLESS_MODEL,
                        "status": "REJECTED",
                        "reason": reason,
                    }
                )
                # Timeouts are ambiguous; do not silently send a second request elsewhere.
                if "timeout" in reason.casefold() or "timed out" in reason.casefold():
                    return self._local(utterance, trace)
        if not self.db.execute("SELECT 1 FROM calls WHERE provider=?", ("OneAPI",)).fetchone():
            try:
                answer = self._attempt("OneAPI", lambda: self._bonus(messages))
                return checked("OneAPI", GLM_MODEL, answer)
            except Exception as e:
                trace.append(
                    {
                        "provider": "OneAPI",
                        "model": GLM_MODEL,
                        "status": "REJECTED",
                        "reason": str(e)[:120],
                    }
                )
        return self._local(utterance, trace)

    def _local(self, utterance, trace):
        local = local_rules(utterance, None)
        validated, _, _ = validate_intent(local, utterance, None)
        if local != validated or validated["intent"] != "search":
            raise AdapterError("ALL_FREE_AND_BONUS_ROUTES_FAILED_SAFE")
        trace.append({"provider": "LOCAL", "model": None, "status": "DETERMINISTIC_VALIDATED"})
        self.history = list(trace)
        actual = self.keyless_post_count + (
            len(self.oneapi_transport.attempts()) if self.oneapi_transport else 0
        )
        return json.dumps(validated, ensure_ascii=False), {
            "source": "LOCAL_AFTER_FREE_RESOURCES_FAILED",
            "provider": "LOCAL",
            "model": None,
            "model_calls": actual,
            "reformboss_calls": 0,
            "automatic_retries": 0,
            "attempts": trace,
            "validated_by": "TRAVEL_DETERMINISTIC_GATE",
        }
