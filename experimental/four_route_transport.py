"""Preserve established routes; add verified InkStone and local Logicc Farm models."""

import json, sqlite3
from contextlib import closing
from pathlib import Path
from provider_transport import CreditTransport, Failure
from inkstone_transport import InkstoneTransport
from logicc_transport import LogiccTransport, logicc_routes

ROOT = Path(__file__).resolve().parents[1]


def inkstone_routes():
    path = ROOT / "INKSTONE_ROUTES.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("routes", {})


class FourRouteTransport:
    def __init__(self, state):
        self.state = Path(state)
        self.legacy = CreditTransport(state)
        self.inkstone = {}
        self.logicc_pool = {}

    def __getattr__(self, name):
        return getattr(self.legacy, name)

    def selected(self, p):
        if p.startswith("Logicc"):
            raise Failure("MODEL_ROUTE_DISABLED")
            routes = logicc_routes()
            if p not in routes:
                raise Failure("LOGICC_ROUTE_NOT_VERIFIED")
            if p not in self.logicc_pool:
                self.logicc_pool[p] = LogiccTransport(
                    self.state / "logicc", model=routes[p]["model"]
                )
            return self.logicc_pool[p]
        routes = inkstone_routes()
        if p not in routes:
            raise Failure("INKSTONE_ROUTE_NOT_VERIFIED")
        if p not in self.inkstone:
            self.inkstone[p] = InkstoneTransport(
                self.state / "inkstone",
                model=routes[p]["model"],
                stage="integration",
                require_free=True,
                disable_thinking=routes[p].get("disable_thinking", False),
            )
        return self.inkstone[p]

    def check_agent_capacity(self, p, steps=4):
        return (
            self.selected(p).check_agent_capacity(p, steps)
            if (p.startswith("InkStone") or p.startswith("Logicc"))
            else self.legacy.check_agent_capacity(p, steps)
        )

    def call(self, p, messages, tools=None, tag=None, max_tokens=None):
        return (
            self.selected(p).call(p, messages, tools, tag, max_tokens)
            if (p.startswith("InkStone") or p.startswith("Logicc"))
            else self.legacy.call(p, messages, tools, tag, max_tokens)
        )

    def rows(self):
        rows = self.legacy.rows()
        path = self.state / "inkstone/inkstone.sqlite"
        if path.is_file():
            with closing(sqlite3.connect(path, timeout=30)) as db:
                rows += [
                    json.loads(r[0])
                    for r in db.execute("SELECT record FROM requests ORDER BY rowid")
                ]
        path_l = self.state / "logicc/logicc.sqlite"
        if path_l.is_file():
            with closing(sqlite3.connect(path_l, timeout=30)) as db:
                rows += [
                    json.loads(r[0])
                    for r in db.execute("SELECT record FROM requests ORDER BY rowid")
                ]
        return rows
