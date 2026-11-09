"""Pre-dispatch-only LLM route rotation. Never retries an emitted inference.
An HTTP error, timeout, reserved/unknown ledger state or malformed inference
is terminal for the current user operation, even when a spare route exists.
"""

import time
from provider_transport import Failure
from .transport import PROFILES
from .task_routing import preferred_routes
from .model_policy import blocked_reason

DEFAULT_ROUTES = ("Gwarden_Gemini4Argon", "IISHKO", "InkStone_AgentsA1")
PREFLIGHT_CODES = {
    "LOGICC_GATEWAY_OFFLINE",
    "LOGICC_MODEL_NOT_ADVERTISED",
    "LOGICC_CATALOG_INVALID",
    "LOGICC_CATALOG_TOO_LARGE",
    "PROVIDER_COOLDOWN",
    "UNRESOLVED_PRIOR_OUTCOME_ROUTE_BLOCKED",
    "PRIOR_UNKNOWN_NO_RETRY",
    "UNKNOWN_NO_RETRY",
    "ACCOUNT_AUTH_STOP",
    "REQUEST_BODY_LIMIT",
    "SKILL_ROUTE_UNKNOWN",
    "REAL_FREE_BALANCE_INSUFFICIENT",
    "INKSTONE_MODEL_NOT_READY",
    "INKSTONE_PRICE_CHANGED_RECONCILE_REQUIRED",
    "FREE_CREDIT_EMPTY",
    "EXISTING_GRANT_INSUFFICIENT",
    "PRICE_UNVERIFIED",
    "RESERVE_BALANCE_TOO_LOW",
    "CIRCUIT_OPEN",
    "PROVIDER_NOT_READY",
    "UNRESOLVED_ACCOUNT_OUTCOME",
    "UNKNOWN_OPERATION_NO_REPLAY",
    "PROVIDER_PREFLIGHT_HTTP_ERROR",
    "PROVIDER_PREFLIGHT_UNAVAILABLE",
    "STOP_REQUESTED",
    "INSUFFICIENT_CALL_ALLOWANCE_FOR_COMPLETE_CYCLE",
    "GWARDEN_KEY_MISSING",
    "GWARDEN_FREE_PROMO_NOT_CONFIRMED",
    "GWARDEN_MODEL_NOT_ADVERTISED",
}


class SafeRotationTransport:
    def __init__(self, delegate, routes=DEFAULT_ROUTES):
        self.delegate = delegate
        self.routes = tuple(routes)
        if (
            not self.routes
            or any(x not in PROFILES for x in self.routes)
            or len(set(self.routes)) != len(self.routes)
        ):
            raise Failure("ROTATION_ROUTES_INVALID")
        self.jev = delegate.jev
        self._sticky = {}

    def rows(self):
        return self.route_rows()

    def route_rows(self):
        return self.delegate.rows()

    def acknowledge_run(self, key):
        return self.delegate.acknowledge_run(key)

    def purge_payloads(self, at=None):
        return self.delegate.purge_payloads(at)

    def route_blocked_reason(self, rows, route, op_hash=None):
        fn = getattr(self.delegate, "route_blocked_reason", None)
        return fn(rows, route, op_hash) if callable(fn) else blocked_reason(rows, route, op_hash)

    def reason_to_skip(self, route):
        configuration = getattr(self.delegate, "route_configuration_reason", None)
        if callable(configuration) and (reason := configuration(route)):
            return reason
        group, model = PROFILES[route]
        try:
            self.delegate.prior_guards(route, model)
        except Failure as exc:
            return str(exc)
        rows = self.route_rows()
        reason = self.route_blocked_reason(rows, route)
        if reason:
            return reason
        health = getattr(self.delegate, "health_reason", None)
        if callable(health) and (reason := health(route)):
            return reason
        if (self.delegate.state / "STOP_INFERENCE").exists():
            return "STOP_REQUESTED"
        return None

    def _restore_run_route(self, run_key):
        # Reconstruct the route from the delegate's persisted request receipts.
        # This is the authoritative state across process/wrapper reconstruction.
        history = [
            r
            for r in self.route_rows()
            if isinstance(r.get("tag"), str) and r["tag"].startswith(run_key + ":")
        ]
        resolved = []
        for r in history:
            route = r.get("route") or r.get("provider")
            if route not in self.routes:
                suffix = r["tag"].rsplit(":", 1)[-1]
                route = suffix if suffix in self.routes else None
            if route:
                resolved.append(route)
        if len(set(resolved)) > 1:
            raise Failure("ROUTE_HISTORY_CONFLICT_NO_REDISTRIBUTION")
        if any(r.get("state") != "RESPONSE_RECEIVED" for r in history):
            raise Failure("PREVIOUS_RUN_DISPATCH_UNRESOLVED_NO_REDISTRIBUTION")
        return resolved[0] if resolved else None

    def call(self, provider, messages, tools=None, tag=None, max_tokens=None):
        run_key = tag.split(":", 1)[0] if isinstance(tag, str) and ":" in tag else None
        persisted = self._restore_run_route(run_key) if run_key else None
        local = self._sticky.get(run_key) if run_key else None
        if local and persisted and local != persisted:
            raise Failure("ROUTE_HISTORY_CONFLICT_NO_REDISTRIBUTION")
        pinned = persisted or local
        if run_key and pinned:
            self._sticky[run_key] = pinned
        if provider == "Auto":
            ranked, complexity = preferred_routes(messages)
            routes = (pinned,) if pinned else tuple(x for x in ranked if x in self.routes)
        else:
            complexity = "MANUAL"
            if provider == "Pollinations":
                raise Failure("POLLINATIONS_DISABLED_IN_THIS_BUILD")
            if provider not in PROFILES:
                raise Failure("SKILL_ROUTE_UNKNOWN")
            routes = (provider,)
            if pinned and pinned != provider:
                raise Failure("PROVIDER_SWITCH_AFTER_DISPATCH_FORBIDDEN")
        skipped = []
        for route in routes:
            reason = self.reason_to_skip(route)
            if reason:
                if pinned:
                    raise Failure("PINNED_ROUTE_UNAVAILABLE_NO_REDISTRIBUTION:" + reason)
                skipped.append({"route": route, "reason": reason, "inference_dispatched": False})
                continue
            dispatch_tag = (tag or "") + ":" + route
            if any(r.get("tag") == dispatch_tag for r in self.route_rows()):
                raise Failure("REQUEST_ID_ALREADY_USED_NO_REDISTRIBUTION")
            try:
                receipt = self.delegate.call(
                    route, messages, tools=tools, tag=dispatch_tag, max_tokens=max_tokens
                )
            except Failure as exc:
                # Ledger is the authority: only a provider refusal BEFORE reservation
                # allows a different route for this *same* operation.
                prior = next((r for r in self.route_rows() if r.get("tag") == dispatch_tag), None)
                if prior is not None or str(exc) not in PREFLIGHT_CODES:
                    raise
                skipped.append({"route": route, "reason": str(exc), "inference_dispatched": False})
                continue
            # Once a model call has been reserved, there is no same-turn fallback.
            if run_key:
                self._sticky[run_key] = route
            receipt = {
                **receipt,
                "route": route,
                "rotation_skips": skipped,
                "automatic_redispatch": False,
                "routing": "PRE_DISPATCH_ONLY",
                "complexity_class": complexity,
            }
            return receipt
        raise Failure(
            "NO_SAFE_MODEL_ROUTE:" + ",".join(x["route"] + ":" + x["reason"] for x in skipped)
        )
