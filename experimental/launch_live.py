"""Opt-in synthetic-only live lab. Ordinary START-DEMO.cmd remains fully offline."""

import argparse, json, time, webbrowser, sqlite3
from pathlib import Path
from app import Application, Handler, ExclusiveHTTPServer
from gateway_transport import GatewayTransport
from live_client import ManagedFallbackClient

ROOT = Path(__file__).resolve().parents[1]
ROUTES = {
    "OneAPI/mimo-v2.6-flash": ("OneAPI", "mimo-v2.6-flash"),
    "OneAPI/glm-5.3-flash": ("OneAPI", "glm-5.3-flash"),
    "Reformboss/deepseek-v4.1-flash": ("Reformboss", "deepseek-v4.1-flash"),
    "Reformboss/kimi-k3": ("Reformboss", "kimi-k3"),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--grant",
        type=Path,
        required=True,
        help="Owner-approved ephemeral synthetic free-credit proof JSON",
    )
    parser.add_argument(
        "--routes",
        default="OneAPI/mimo-v2.6-flash",
        help="Semicolon-separated route IDs in priority order",
    )
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    routes = [x.strip() for x in args.routes.split(";")]
    if not routes or any(x not in ROUTES for x in routes):
        raise SystemExit("UNKNOWN_ROUTE")
    grant = json.loads(args.grant.read_text(encoding="utf-8"))
    if (
        grant.get("owner_authorized") is not True
        or grant.get("synthetic_only") is not True
        or grant.get("mode") != "LIVE_TEST"
    ):
        raise SystemExit("EXPLICIT_SYNTHETIC_GRANT_REQUIRED")
    proofs = grant.get("free_credit_proofs", {})
    for route in routes:
        proof = proofs.get(route, grant.get("free_credit_proof", {}))
        if (
            not proof
            or proof.get("model") != ROUTES[route][1]
            or proof.get("provider") != ROUTES[route][0]
            or proof.get("covered_by_free_credit") is not True
            or not 0 <= time.time() - proof.get("observed_unix", 0) <= 300
        ):
            raise SystemExit("CURRENT_FREE_ELIGIBILITY_NOT_PROVEN_" + route)
        if route not in grant.get("pricing_per_token", {}):
            raise SystemExit("CURRENT_PRICE_UNKNOWN_" + route)
    # Do not silently escape the unresolved diagnostic attempt from the prior PLUS build.
    historical = ROOT / "runs/plus_live_attempts.sqlite"
    if historical.exists():
        with sqlite3.connect(historical) as db:
            unresolved = {
                (x[0], x[1])
                for x in db.execute(
                    "SELECT provider,model FROM calls WHERE status='UNKNOWN_OUTCOME'"
                )
            }
        if any(ROUTES[r] in unresolved for r in routes):
            raise SystemExit("HISTORICAL_UNKNOWN_OUTCOME_UNRESOLVED_USE_ANOTHER_VERIFIED_ROUTE")
    ledger = ROOT / "experimental/runtime/live_lab_attempts.sqlite"
    transport = GatewayTransport(grant, ledger, timeout_seconds=65)
    client = ManagedFallbackClient([ROUTES[r] for r in routes], transport)
    app = Application(ROOT / "experimental/runtime/live_lab_sessions", client)
    app.live_enabled = True
    Handler.app = app
    server = ExclusiveHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print("SYNTHETIC_LIVE_LAB: " + url, flush=True)
    print("Only /api/live/sample may call live models; normal text is local.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
        transport.close()


if __name__ == "__main__":
    main()
