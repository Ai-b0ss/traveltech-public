"""OneAPI GLM real E2E after LOCAL injected keyless 402. No Pollinations network."""

import sys, argparse, webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "experimental"), str(ROOT / "prototype"), str(ROOT / "prototype/base")]
from app import Application, Handler, ExclusiveHTTPServer
from ai_router import AdapterError
from natural_language import NaturalLanguage
from free_resources_client import FreeResourcesClient


def injected_keyless_402(messages):
    raise AdapterError("HTTP 402 INJECTED_NO_PUBLIC_NETWORK")


def start(port=8816, open_browser=False):
    client = FreeResourcesClient(
        ROOT / "experimental/runtime/free_bonus_attempts.sqlite",
        allow_network=True,
        keyless_fake=injected_keyless_402,
    )
    app = Application(ROOT / "experimental/runtime/free_bonus_sessions", client)
    app.live_enabled = True
    app.free_language = NaturalLanguage(app, client)
    Handler.app = app
    server = ExclusiveHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:" + str(server.server_address[1]) + "/"
    print("FREE_BONUS_GLМ_LAB=" + url, flush=True)
    print(
        "PUBLIC_402_INJECTED_NO_NETWORK; ONE_REAL_ONEAPI_GLM_AT_MOST; ZERO_PAID_BALANCE_REQUIRED",
        flush=True,
    )
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        app.close()
        client.close()


if __name__ == "__main__":
    arg = argparse.ArgumentParser()
    arg.add_argument("--port", type=int, default=8816)
    arg.add_argument("--no-browser", action="store_true")
    a = arg.parse_args()
    start(a.port, not a.no_browser)
