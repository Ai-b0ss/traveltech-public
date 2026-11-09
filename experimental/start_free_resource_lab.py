"""Isolated TravelTech free-resource synthetic lab. No inference at startup.

Public requests: only one Pollinations try, then at most one OneAPI/GLM
credited request if live free credit and zero paid balance are confirmed.
"""

import sys, argparse, webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "experimental"), str(ROOT / "prototype"), str(ROOT / "prototype/base")]
from app import Application, Handler, ExclusiveHTTPServer
from free_resources_client import FreeResourcesClient
from natural_language import NaturalLanguage


def main(port=8815, open_browser=True):
    client = FreeResourcesClient(ROOT / "experimental/runtime/freelab_attempts.sqlite")
    app = Application(ROOT / "experimental/runtime/freelab_sessions", client)
    app.live_enabled = True
    app.free_language = NaturalLanguage(app, client)
    Handler.app = app
    server = ExclusiveHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:" + str(server.server_address[1]) + "/"
    print("FREE_RESOURCE_LAB=" + url, flush=True)
    print("NO_MODEL_CALL_AT_START; NO_REFORMBOSS; SYNTHETIC_ONLY", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        app.close()
        client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8815)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    main(args.port, not args.no_browser)
