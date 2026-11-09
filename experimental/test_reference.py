"""Behavior/invariant tests for the final reference, separate from archived tests."""

import unittest, tempfile, sys, json, time, sqlite3, concurrent.futures
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, str(Path(__file__).parent))
from app import Application, InputError, Conflict
from budget_math import resolve_budget
from natural_language import validate_intent
from ai_router import ChatAdapter, JevAdapter, RouteHealth, retry_seconds, SCENARIOS


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.apps = []
        self.app = self.new_app()

    def new_app(self):
        app = Application(self.tmp.name)
        self.apps.append(app)
        return app

    def tearDown(self):
        for app in self.apps:
            app.close()
        self.tmp.cleanup()

    def trip(self):
        return self.app.execute("start", name="ru")

    def preview(self, trip, text="Снизь бюджет до 20000 рублей"):
        return self.app.language.execute(text, "local", trip["session_id"], trip["revision"], True)[
            "preview"
        ]

    def test_cancel_restart_forever(self):
        t = self.trip()
        p = self.preview(t)
        self.app.cancel_preview(t["session_id"], p["preview_id"])
        restarted = self.new_app()
        with self.assertRaises(Conflict):
            restarted.commit_proposal(t["session_id"], p["preview_id"], 0)
        self.assertEqual(restarted.execute("read", sid=t["session_id"])["request"], t["request"])

    def test_cross_session(self):
        a = self.trip()
        b = self.trip()
        p = self.preview(a)
        with self.assertRaises(InputError):
            self.app.commit_proposal(b["session_id"], p["preview_id"], 0)
        self.assertEqual(self.app.execute("read", sid=b["session_id"])["request"], b["request"])
        with self.assertRaises(InputError):
            self.app.cancel_preview(b["session_id"], p["preview_id"])
        self.app.commit_proposal(a["session_id"], p["preview_id"], 0)

    def test_double_commit_and_undo_revision(self):
        t = self.trip()
        p = self.preview(t)
        out = self.app.commit_proposal(t["session_id"], p["preview_id"], 0)
        self.assertEqual(out["revision"], 1)
        with self.assertRaises(Conflict):
            self.app.commit_proposal(t["session_id"], p["preview_id"], 0)
        undo = self.app.execute("undo", sid=t["session_id"], revision=1)
        self.assertEqual(undo["revision"], 2)
        self.assertEqual(undo["request"], t["request"])

    def test_superseded_and_expired(self):
        t = self.trip()
        old = self.preview(t)
        new = self.preview(t, "Увеличь бюджет на 15 процентов")
        with self.assertRaises(Conflict):
            self.app.commit_proposal(t["session_id"], old["preview_id"], 0)
        self.app.store.db.execute("UPDATE proposals SET expires=0 WHERE id=?", (new["preview_id"],))
        with self.assertRaises(Conflict):
            self.app.commit_proposal(t["session_id"], new["preview_id"], 0)

    def test_tampering_mutable_return_and_disk(self):
        t = self.trip()
        p = self.preview(t)
        identifier = p["preview_id"]
        p["patch"]["budget"] = "1"
        self.assertEqual(
            self.app.commit_proposal(t["session_id"], identifier, 0)["request"]["budget"], "20000"
        )
        t = self.trip()
        p = self.preview(t)
        body = self.app.store.db.execute(
            "SELECT body FROM proposals WHERE id=?", (p["preview_id"],)
        ).fetchone()[0]
        self.app.store.db.execute(
            "UPDATE proposals SET body=? WHERE id=?", (body.replace("20000", "1"), p["preview_id"])
        )
        with self.assertRaises(InputError):
            self.app.commit_proposal(t["session_id"], p["preview_id"], 0)

    def test_new_search_two_phases_always(self):
        for flag in (True, False):
            p = self.app.language.execute(
                "Подбери отель в Сочи с 26 по 28 октября 2026 года для двух взрослых, один номер, бюджет до 30 тысяч рублей. Желательно с завтраком",
                "local",
                preview=flag,
            )
            self.assertIsNone(p["session_id"])
            self.assertTrue(p["has_preview"])
            self.assertEqual(
                self.app.store.db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0
            )
            self.app.cancel_preview(None, p["preview_id"])

    def test_confirm_new_then_reload(self):
        p = self.app.language.execute(
            "Подбери отель в Сочи с 6 по 9 ноября 2026 года, 2 взрослых, 1 номер, бюджет до 30000 рублей",
            "local",
        )
        out = self.app.commit_proposal(None, p["preview_id"], None)
        self.assertEqual(out["request"]["checkin"], "2026-11-06")
        self.assertTrue(all("REQUEST_CONTEXT_MISMATCH" in c["reasons"] for c in out["cards"]))
        self.assertTrue(all(c["status"] == "REJECT" for c in out["cards"]))
        self.assertEqual(
            self.new_app().execute("read", sid=out["session_id"])["request"], out["request"]
        )

    def test_preview_restores_after_restart(self):
        t = self.trip()
        p = self.preview(t)
        again = self.new_app()
        out = again.execute("read", sid=t["session_id"])
        self.assertEqual(out["pending_preview"]["preview_id"], p["preview_id"])
        self.assertEqual(out["request"]["budget"], "30000")
        self.assertEqual(
            again.commit_proposal(t["session_id"], p["preview_id"], 0)["request"]["budget"], "20000"
        )

    def test_transaction_failure_no_partial_commit(self):
        t = self.trip()
        p = self.preview(t)
        save = self.app._save

        def fail(*args):
            raise OSError("injected disk failure")

        self.app._save = fail
        with self.assertRaises(OSError):
            self.app.commit_proposal(t["session_id"], p["preview_id"], 0)
        self.app._save = save
        self.assertEqual(self.app.execute("read", sid=t["session_id"])["request"], t["request"])
        self.assertEqual(
            self.app.commit_proposal(t["session_id"], p["preview_id"], 0)["request"]["budget"],
            "20000",
        )

    def test_budget_preserves_all_other_fields(self):
        t = self.trip()
        p = self.preview(
            t,
            "Увеличь бюджет на 15%, остальные условия не трогай, но покажи, какие предложения всё ещё требуют подтверждения",
        )
        out = self.app.commit_proposal(t["session_id"], p["preview_id"], 0)
        self.assertEqual(out["request"]["budget"], "34500")
        self.assertEqual(
            {k: v for k, v in out["request"].items() if k != "budget"},
            {k: v for k, v in t["request"].items() if k != "budget"},
        )
        self.assertTrue(all(c["inventory"] == "NOT_VERIFIED" for c in out["cards"]))

    def test_breakfast_gate_and_undo(self):
        t = self.trip()
        p = self.preview(t, "Нужен завтрак")
        self.assertTrue(p["prospective_request"]["breakfast"])
        out = self.app.commit_proposal(t["session_id"], p["preview_id"], 0)
        self.assertTrue(all(c["status"] == "REJECT" for c in out["cards"]))
        with self.assertRaises(InputError):
            validate_intent(
                {"intent": "constraints", "values": {"breakfast": False}, "needs": []},
                "Нужен завтрак",
                self.app._load(t["session_id"])[1].state.request,
            )

    def test_live_default_blocked(self):
        t = self.trip()
        with self.assertRaises(ValueError):
            self.app.language.execute(
                "Снизь бюджет до 10000 рублей", "real", t["session_id"], 0, True
            )
        self.assertEqual(self.app.execute("read", sid=t["session_id"])["request"], t["request"])

    def test_multiroom_allocation_and_two_cities_clarify(self):
        for text in (
            "Подбери отель в Сочи с 26 по 28 октября 2026 года, 2 взрослых, 2 номера, бюджет до 30000 рублей",
            "Подбери отель в Сочи или Дубае с 26 по 28 октября 2026 года, 2 взрослых, 1 номер, бюджет до 30000 рублей",
        ):
            out = self.app.language.execute(text, "local")
            self.assertIsNone(out["session_id"])
            self.assertEqual(out["language"]["validated_intent"]["intent"], "clarify")

    def test_revision_type(self):
        t = self.trip()
        for revision in (False, "0", None, 1):
            with self.assertRaises(Conflict):
                self.app.preview_proposal(t["session_id"], {"budget": "5000"}, revision)

    def test_preference_confirm_restart_undo(self):
        t = self.trip()
        p = self.preview(t, "Желательно с завтраком")
        self.assertEqual(self.app.execute("read", sid=t["session_id"])["preferences"], [])
        out = self.app.commit_proposal(t["session_id"], p["preview_id"], 0)
        self.assertFalse(out["request"]["breakfast"])
        self.assertEqual(out["request"], t["request"])
        self.assertEqual(out["preferences"][0]["evidence_status"], "UNVERIFIED")
        again = self.new_app()
        self.assertEqual(
            again.execute("read", sid=t["session_id"])["preferences"], out["preferences"]
        )
        self.assertEqual(again.execute("undo", sid=t["session_id"], revision=1)["preferences"], [])

    def test_random_invalid_ids(self):
        for identifier in ("../state.sqlite", "f" * 63, "f" * 65, "💥" * 64, None):
            with self.assertRaises(ValueError):
                self.app.commit_proposal(None, identifier, None)

    def test_concurrent_double_commit(self):
        t = self.trip()
        p = self.preview(t)
        apps = [self.new_app() for _ in range(5)]

        def attempt(app):
            try:
                return app.commit_proposal(t["session_id"], p["preview_id"], 0)["revision"]
            except Conflict:
                return "REJECTED"

        with concurrent.futures.ThreadPoolExecutor(5) as pool:
            results = list(pool.map(attempt, apps))
        self.assertEqual(results.count(1), 1)
        self.assertEqual(results.count("REJECTED"), 4)

    def test_all_shadow_failures_no_mutation(self):
        t = self.trip()
        for name in SCENARIOS:
            r = self.app.router.simulate(name)
            self.assertEqual(r["external_calls"], 0)
            self.assertFalse(r["alternative_state_applied"])
            self.assertEqual(self.app.execute("read", sid=t["session_id"])["request"], t["request"])

    def test_chat_adapter_orchestration_fixture_and_semantic_gate(self):
        from ai_router import LanguageModelClient

        def fixture(endpoint, payload):
            self.assertEqual(endpoint, "https://api.reformboss.com/v1/chat/completions")
            result = {"intent": "budget", "values": {"budget": "20000"}, "needs": []}
            return {
                "model": "glm-5.3",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
            }

        app = Application(
            Path(self.tmp.name) / "adapter",
            LanguageModelClient(ChatAdapter("Reformboss", "glm-5.3", fixture)),
        )
        self.apps.append(app)
        t = app.execute("start", name="ru")
        p = app.language.execute("Снизь бюджет до 20000 рублей", "real", t["session_id"], 0, True)
        self.assertTrue(p["has_preview"])
        self.assertEqual(p["language"]["source"], "LOCAL_ADAPTER_FIXTURE")
        self.assertEqual(app.execute("read", sid=t["session_id"])["request"], t["request"])
        with self.assertRaises(InputError):
            app.language.execute("Снизь бюджет до 15000 рублей", "real", t["session_id"], 0, True)


class MoneyAndAdapterTests(unittest.TestCase):
    def test_money_cases(self):
        cases = [
            ("Увеличь бюджет на 15 процентов", "34500"),
            ("Снизь бюджет на 12,5%", "26250"),
            ("Увеличь бюджет на 0,125%", "30037.5"),
            ("Снизь бюджет в три раза", "10000"),
            ("Увеличь бюджет в 1,5 раза", "45000"),
            ("Снизь бюджет на 5 тысяч рублей до 25 тысяч", "25000"),
            ("Снизь бюджет на 5 тысяч до 20 тысяч", "CONFLICT"),
            ("Увеличь бюджет на 15% рублей", "CONFLICT"),
            ("Уменьши бюджет на 100%", "INVALID"),
            ("Увеличь бюджет на -1%", "INVALID"),
            ("Снизь бюджет на 10% и увеличь на 20%", "CONFLICT"),
            ("Бюджет 20 тысяч или 30 тысяч", "CONFLICT"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(resolve_budget(text, "30000"), expected)
        self.assertEqual(resolve_budget("Увеличь бюджет на 1%", "10.5"), "10.61")

    def test_chat_rejects_identity_length_duplicate(self):
        a = ChatAdapter("OneAPI", "mimo-v2.6-flash")
        for response in (
            {"model": "wrong"},
            {"model": a.model, "choices": [{"finish_reason": "length"}]},
            {
                "model": a.model,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"intent":"inspect","intent":"search","values":{},"needs":[]}'
                        },
                    }
                ],
            },
        ):
            with self.assertRaises(ValueError):
                a.decode(response)

    def test_jev_separate_contract(self):
        a = JevAdapter()
        payload = a.payload("Уточни даты")
        self.assertIn("questions", payload)
        self.assertNotIn("messages", payload)
        with self.assertRaises(ValueError):
            a.decode(
                {
                    "model": a.model,
                    "answers": {
                        "next_action": {"type": "choice", "choice": "INJECT", "probabilities": {}}
                    },
                }
            )

    def test_route_health(self):
        h = RouteHealth()
        h.failure(429, 100, "17")
        self.assertFalse(h.allowed(116))
        self.assertTrue(h.allowed(117))
        h.failure(503, 200)
        self.assertFalse(h.allowed(201))
        h.failure(402, 300)
        self.assertFalse(h.allowed(100000))
        self.assertEqual(retry_seconds("Wed, 21 Oct 2015 07:28:00 GMT", 1445412470), 10)

    def test_live_transport_budget_journal_without_network(self):
        from gateway_transport import GatewayTransport

        with tempfile.TemporaryDirectory() as folder:
            endpoint = "https://api.oneapi.id/v1/chat/completions"
            payload = {
                "model": "mimo-v2.6-flash",
                "messages": [{"role": "user", "content": "SYNTHETIC"}],
                "max_tokens": 100,
            }
            g = {
                "mode": "LIVE_TEST",
                "owner_authorized": True,
                "synthetic_only": True,
                "total_limit_usd": ".10",
                "free_credit_proof": {
                    "covered_by_free_credit": True,
                    "observed_unix": time.time(),
                    "free_credit_usd": "1",
                },
                "pricing_per_token": {
                    "OneAPI/mimo-v2.6-flash": {"input": "0.000001", "output": "0.000002"}
                },
            }
            t = GatewayTransport(g, Path(folder) / "ledger.sqlite")
            try:
                identifier, _ = t.reserve(endpoint, payload)
                self.assertEqual(t.db.execute("SELECT status FROM calls").fetchone()[0], "RESERVED")
                with self.assertRaises(ValueError):
                    t.reserve(endpoint, payload)
                t.db.execute("UPDATE calls SET status=?", ("RESPONSE_RECEIVED",))
                # Receipt might be durable even when crash prevented delivery.
                with self.assertRaises(ValueError):
                    t.reserve(endpoint, payload)
                for i in range(4):
                    varied = {
                        **payload,
                        "messages": [{"role": "user", "content": f"SYNTHETIC_VARIANT_{i}"}],
                    }
                    t.reserve(endpoint, varied)
                    t.db.execute("UPDATE calls SET status=?", ("RESPONSE_RECEIVED",))
                sixth = {
                    **payload,
                    "messages": [{"role": "user", "content": "SIXTH_DISTINCT_REQUEST"}],
                }
                with self.assertRaises(ValueError):
                    t.reserve(endpoint, sixth)
            finally:
                t.close()
            stale = GatewayTransport(
                {
                    **g,
                    "free_credit_proof": {
                        **g["free_credit_proof"],
                        "observed_unix": time.time() - 1000,
                    },
                },
                Path(folder) / "stale.sqlite",
            )
            try:
                with self.assertRaises(ValueError):
                    stale.reserve(endpoint, payload)
                self.assertEqual(stale.db.execute("SELECT count(*) FROM calls").fetchone()[0], 0)
            finally:
                stale.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
