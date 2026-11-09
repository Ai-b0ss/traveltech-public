"""One local entry point, including an already-running instance check."""

import argparse, urllib.request, json, webbrowser, threading, sys, hashlib, os
from pathlib import Path
from app import Application, Handler, ExclusiveHTTPServer, ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--port", type=int, default=8796)
    parser.add_argument("--storage", type=Path, default=Path(__file__).parent / "runtime")
    args = parser.parse_args()
    server = None
    # Check before binding: on Windows SO_REUSEADDR may otherwise allow a second listener.
    if args.port:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{args.port}/api/health", timeout=1
            ) as response:
                health = json.load(response)
            if (
                health.get("app") == "TRAVELTECH_REFERENCE"
                and health.get("root_id") == hashlib.sha256(str(ROOT).encode()).hexdigest()
            ):
                url = f"http://127.0.0.1:{args.port}/"
                print("Уже запущено: " + url, flush=True)
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
        except Exception:
            pass
    try:
        server = ExclusiveHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        server = ExclusiveHTTPServer(("127.0.0.1", 0), Handler)
    Handler.app = Application(args.storage)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print("TravelTech: " + url, flush=True)
    if args.smoke:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                url + "api/action",
                data=json.dumps({"action": "start", "name": "ru"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                data = json.load(response)
            assert len(data["cards"]) == 3 and all(c["status"] == "REVIEW" for c in data["cards"])
            print("LAUNCH_SMOKE_PASS", flush=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            Handler.app.close()
        return 0
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        Handler.app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
