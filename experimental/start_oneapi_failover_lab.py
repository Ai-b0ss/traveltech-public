"""Isolated synthetic OneAPI failover browser lab. One MiMo request maximum.

Simulates GLM 503 locally without a GLM POST, then sends one real MiMo
request only after a fresh read-only OneAPI free-credit check.
"""

import json, time, webbrowser, argparse, sys, datetime
from pathlib import Path
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "experimental"),
    str(ROOT / "audit"),
    str(ROOT / "prototype"),
    str(ROOT / "prototype/base"),
]
from current_oneapi_preflight_20261014 import main as account_check
from gateway_transport import GatewayTransport
from ai_router import AdapterError
from failover_probe import OneAPIProbeRouter, ProbeTransport, SECONDARY
from app import Application, Handler, ExclusiveHTTPServer

FREE_PROOF_PATH = ROOT / "runs/ONEAPI_READONLY_PREFLIGHT_20261014.json"
ROUTE = "OneAPI/" + SECONDARY
LEDGER = ROOT / "experimental/runtime/ONEAPI_MIMO_VERIFIED_E2E_ATTEMPTS_2.sqlite"


def read_fresh_proof(model):
    if model != SECONDARY:
        raise AdapterError("UNAUTHORIZED_MODEL")
    if account_check() != 0:
        raise AdapterError("ONEAPI_READONLY_PREFLIGHT_FAILED")
    info = json.loads(FREE_PROOF_PATH.read_text(encoding="utf8"))
    at = datetime.datetime.fromisoformat(info["observed_at"]).timestamp()
    if (
        not 0 <= time.time() - at <= 180
        or info.get("status") != "active"
        or not info.get("ready_to_grant")
    ):
        raise AdapterError("ONEAPI_FREE_CREDIT_NOT_PROVEN")
    if Decimal(str(info.get("balance_usd", "-1"))) != 0 or Decimal(
        str(info["free_credit_usd"])
    ) < Decimal(".003"):
        raise AdapterError("ONEAPI_FREE_CREDIT_NOT_PROVEN")
    row = next((m for m in info.get("models", []) if m.get("id") == model), None)
    if not row or not row.get("prompt") or not row.get("completion"):
        raise AdapterError("ONEAPI_MODEL_PRICE_NOT_VERIFIED")
    return {
        "model": model,
        "provider": "OneAPI",
        "observed_unix": at,
        "free_credit_usd": info["free_credit_usd"],
        "covered_by_free_credit": True,
        "input": row["prompt"],
        "output": row["completion"],
    }


class ReplayOnlyClient:
    def infer(self, messages):
        raise AdapterError("LIVE_NETWORK_DISABLED_REPLAY_ONLY")

    def status(self):
        return {
            "mode": "REPLAY_ONLY",
            "real_enabled": False,
            "remaining_requests": 0,
            "attempt_count": 0,
            "last_transport_status": "NO_NETWORK_BY_DESIGN",
            "routes": [
                {"provider": "OneAPI", "model": "mimo-v2.6-flash", "role": "RECORDED_RESPONSE_ONLY"}
            ],
            "reformboss_enabled": False,
            "automatic_retries": 0,
        }


def start(port=8803, open_browser=True, replay_only=False):
    backing = None
    if replay_only:
        client = ReplayOnlyClient()
        storage = ROOT / "experimental/runtime/ONEAPI_MIMO_VERIFIED_E2E_REPLAY_SESSIONS"
    else:
        proof = read_fresh_proof(SECONDARY)
        grant = {
            "mode": "LIVE_TEST",
            "owner_authorized": True,
            "synthetic_only": True,
            "total_limit_usd": ".003",
            "max_total_requests": 1,
            "free_credit_proofs": {
                ROUTE: {
                    k: proof[k]
                    for k in (
                        "provider",
                        "model",
                        "observed_unix",
                        "free_credit_usd",
                        "covered_by_free_credit",
                    )
                }
            },
            "pricing_per_token": {ROUTE: {"input": proof["input"], "output": proof["output"]}},
        }
        gateway = GatewayTransport(grant, LEDGER, timeout_seconds=90)

        def fresh(model):
            verified = read_fresh_proof(model)
            grant["free_credit_proofs"][ROUTE] = {
                k: verified[k]
                for k in (
                    "provider",
                    "model",
                    "observed_unix",
                    "free_credit_usd",
                    "covered_by_free_credit",
                )
            }
            grant["pricing_per_token"][ROUTE] = {
                "input": verified["input"],
                "output": verified["output"],
            }

        backing = ProbeTransport(gateway, fresh)
        client = OneAPIProbeRouter(backing)
        storage = ROOT / "experimental/runtime/ONEAPI_MIMO_VERIFIED_E2E_SESSIONS"
    app = Application(storage, client)
    app.live_enabled = True
    from mimo_replay_client import RecordedMimoReplayClient
    from natural_language import NaturalLanguage

    app.replay_language = NaturalLanguage(
        app, RecordedMimoReplayClient(ROOT / "BENCHMARK_20261014/attempts.jsonl")
    )
    from local_recovery_demo import LocalRecoveryDemoClient

    app.recovery_language = NaturalLanguage(app, LocalRecoveryDemoClient())
    Handler.app = app
    server = ExclusiveHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:" + str(server.server_address[1]) + "/"
    print("ONEAPI_FAILOVER_LAB=" + url, flush=True)
    print(
        "REPLAY_ONLY_NO_NETWORK" if replay_only else "GLM_503_INJECTED; ONE_REAL_MIMO_MAX",
        flush=True,
    )
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        app.close()
        if backing:
            backing.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8803)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--replay-only", action="store_true")
    args = ap.parse_args()
    start(args.port, not args.no_browser, args.replay_only)
