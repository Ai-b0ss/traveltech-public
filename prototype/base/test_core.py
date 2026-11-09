import unittest, json, random, time
from datetime import datetime, timezone, timedelta
from dataclasses import replace, asdict
from decimal import Decimal
from pathlib import Path
from travel_core import *
from adapters import *

NOW = datetime(2026, 10, 13, 11, 47, tzinfo=timezone.utc)
REQ = Request("Moscow", "RU", "2026-10-26", "2026-10-28", (Guests(2),), "1000", "RUB")
BASE = Offer(
    "FIXTURE",
    "h",
    "room1",
    "rate1",
    "Moscow",
    "RU",
    REQ.checkin,
    REQ.checkout,
    REQ.rooms,
    "RUB",
    "100",
    "PER_ROOM_PER_NIGHT",
    True,
    "SYNTHETIC",
    NOW,
    "synthetic:test",
    capacity=2,
)


class CoreTests(unittest.TestCase):
    def run_offer(self, o=BASE, r=REQ, **kwargs):
        return Engine(ListProvider([o]), **kwargs).search(r, NOW)

    def d(self, o=BASE, r=REQ):
        return self.run_offer(o, r)["decisions"][0]

    def test_normal(self):
        d = self.d()
        self.assertEqual(d.status, "CONSTRAINTS_MET_NOT_LIVE")
        self.assertEqual(d.base_total, "200")
        self.assertEqual(d.inventory, "NOT_VERIFIED")

    def test_total_not_multiplied(self):
        self.assertEqual(
            self.d(replace(BASE, basis="FULL_STAY_ALL_ROOMS", amount="200")).base_total, "200"
        )

    def test_multiple_rooms(self):
        req = replace(REQ, rooms=(Guests(1), Guests(1)))
        self.assertEqual(self.d(replace(BASE, guests=req.rooms), req).base_total, "400")

    def test_tax_unknown(self):
        self.assertEqual(self.d(replace(BASE, taxes_known=None)).status, "REVIEW")

    def test_tax_false(self):
        self.assertEqual(self.d(replace(BASE, taxes_known=False)).status, "REVIEW")

    def test_tax_type_regression(self):
        for v in ("false", 0, 1, [], {}):
            with self.subTest(v=v):
                self.assertEqual(self.run_offer(replace(BASE, taxes_known=v))["status"], "PARTIAL")

    def test_empty_source_validates_request(self):
        for req in (
            replace(REQ, checkin="2026-02-30"),
            replace(REQ, rooms=()),
            replace(REQ, budget="-1"),
            replace(REQ, currency="ABC"),
        ):
            with self.subTest(req=req), self.assertRaises(InputError):
                Engine(ListProvider([])).search(req, NOW)

    def test_empty_source_good(self):
        self.assertEqual(Engine(ListProvider([])).search(REQ, NOW)["decisions"], [])

    def test_room_rate_identity_regression(self):
        r = Engine(
            ListProvider([BASE, replace(BASE, rate_id="rate2"), replace(BASE, room_id="room2")])
        ).search(REQ, NOW)
        self.assertEqual(len(r["decisions"]), 3)
        self.assertFalse(r["issues"])

    def test_duplicate_exact(self):
        self.assertIn(
            "DUPLICATE_EXACT_OFFER", Engine(ListProvider([BASE, BASE])).search(REQ, NOW)["issues"]
        )

    def test_missing_price_not_zero(self):
        self.assertEqual(self.d(replace(BASE, amount=None)).base_total, None)

    def test_unknown_currency(self):
        self.assertEqual(self.d(replace(BASE, currency="CNY")).status, "REVIEW")

    def test_cny_supported(self):
        self.assertEqual(
            self.d(replace(BASE, currency="CNY"), replace(REQ, currency="CNY")).status,
            "CONSTRAINTS_MET_NOT_LIVE",
        )

    def test_minor_currency_units(self):
        self.assertTrue(
            self.run_offer(
                replace(BASE, currency="JPY", amount="1.23"), replace(REQ, currency="JPY")
            )["issues"]
        )
        self.assertEqual(
            self.d(
                replace(BASE, currency="KWD", amount="1.234"), replace(REQ, currency="KWD")
            ).base_total,
            "2.468",
        )

    def test_stale(self):
        self.assertIn(
            "STALE_OR_FUTURE_SOURCE",
            self.d(replace(BASE, observed_at=NOW - timedelta(days=3))).reasons,
        )

    def test_future(self):
        self.assertIn(
            "STALE_OR_FUTURE_SOURCE",
            self.d(replace(BASE, observed_at=NOW + timedelta(seconds=1))).reasons,
        )

    def test_naive_clock(self):
        with self.assertRaises(InputError):
            Engine(ListProvider([])).search(REQ, NOW.replace(tzinfo=None))

    def test_price_float_rejected(self):
        self.assertTrue(self.run_offer(replace(BASE, amount=1.2))["issues"])

    def test_nan_rejected(self):
        self.assertTrue(self.run_offer(replace(BASE, amount="NaN"))["issues"])

    def test_huge_exponent_rejected(self):
        self.assertTrue(self.run_offer(replace(BASE, amount="1e999999"))["issues"])

    def test_over_budget(self):
        self.assertEqual(self.d(replace(BASE, amount="600")).status, "REJECT")

    def test_zero_not_missing(self):
        self.assertEqual(self.d(replace(BASE, amount="0")).base_total, "0")

    def test_unknown_capacity(self):
        self.assertEqual(self.d(replace(BASE, capacity=None)).status, "REVIEW")

    def test_capacity_exceeded(self):
        self.assertEqual(self.d(replace(BASE, capacity=1)).status, "REJECT")

    def test_children_age(self):
        for age in (-1, 18, True):
            with self.assertRaises(InputError):
                replace(REQ, rooms=(Guests(2, (age,)),)).validate()

    def test_child_request_binding(self):
        self.assertIn(
            "REQUEST_CONTEXT_MISMATCH", self.d(BASE, replace(REQ, rooms=(Guests(2, (6,)),))).reasons
        )

    def test_breakfast_unknown(self):
        self.assertEqual(self.d(BASE, replace(REQ, breakfast=True)).status, "REVIEW")

    def test_breakfast_rejected(self):
        self.assertEqual(
            self.d(replace(BASE, breakfast=False), replace(REQ, breakfast=True)).status, "REJECT"
        )

    def test_cancel_timezone(self):
        deadline = datetime(2026, 10, 23, 8, tzinfo=timezone.utc)
        self.assertEqual(
            self.d(
                replace(BASE, cancel_until=datetime.fromisoformat("2026-10-23T16:00:00+08:00")),
                replace(REQ, free_cancel_until=deadline),
            ).status,
            "CONSTRAINTS_MET_NOT_LIVE",
        )

    def test_cancel_too_early(self):
        self.assertEqual(
            self.d(
                replace(BASE, cancel_until=NOW),
                replace(REQ, free_cancel_until=NOW + timedelta(days=1)),
            ).status,
            "REJECT",
        )

    def test_fee_selected_over_cap(self):
        self.assertEqual(
            self.d(
                replace(BASE, fee="900", fee_method="STRIPE"), replace(REQ, payment_method="STRIPE")
            ).status,
            "REJECT",
        )

    def test_fee_unknown_method(self):
        self.assertEqual(self.d(replace(BASE, fee="10", fee_method="STRIPE")).status, "REVIEW")

    def test_fee_separate(self):
        d = self.d(
            replace(BASE, fee="10", fee_method="STRIPE"), replace(REQ, payment_method="STRIPE")
        )
        self.assertEqual(d.base_total, "200")
        self.assertEqual(d.payable_for_method, "210")

    def test_geography_switch(self):
        with self.assertRaises(InputError):
            replace(REQ, country="AE", allowed_countries=("RU",)).validate()
        self.assertEqual(self.d(replace(BASE, country="AE")).status, "REJECT")

    def test_environment_cohorts_separate(self):
        r = Engine(
            ListProvider([BASE, replace(BASE, rate_id="other", evidence="ARCHIVED_RESPONSE")])
        ).search(REQ, NOW)
        self.assertEqual(len(r["ranked_cohorts"]), 2)

    def test_source_failure(self):
        class Failing:
            def search(self, request):
                raise TimeoutError()

        self.assertEqual(Engine(Failing()).search(REQ, NOW)["status"], "SOURCE_ERROR")

    def test_invalid_source_collection(self):
        class Bad:
            def search(self, request):
                return None

        self.assertEqual(Engine(Bad()).search(REQ, NOW)["status"], "SOURCE_ERROR")

    def test_candidate_limit(self):
        r = Engine(
            ListProvider([replace(BASE, rate_id=str(i)) for i in range(10)]), max_candidates=5
        ).search(REQ, NOW)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["scanned"], 5)

    def test_deadline_reports_no_interrupt_guarantee(self):
        class Slow:
            def search(self, request):
                time.sleep(0.003)
                return [BASE]

        self.assertEqual(Engine(Slow(), total_limit_ms=1).search(REQ, NOW)["status"], "TIME_LIMIT")

    def test_unicode_regression(self):
        for raw in (chr(0xD800), '"\\ud800"', b"\xff"):
            with self.assertRaises(InputError):
                safe_json(raw)

    def test_duplicate_json(self):
        with self.assertRaises(InputError):
            safe_json('{"tax":false,"tax":true}')

    def test_deep_json(self):
        with self.assertRaises(InputError):
            safe_json("[" * 2000 + "0" + "]" * 2000)

    def test_nonfinite_json(self):
        with self.assertRaises(InputError):
            safe_json('{"x":NaN}')

    def test_context_price_order_changes(self):
        a = replace(BASE, amount="200")
        b = replace(BASE, rate_id="b", amount="100")
        r = Engine(ListProvider([a, b])).search(REQ, NOW)
        self.assertEqual(next(iter(r["ranked_cohorts"].values()))[0].identity[3], "b")

    def test_alternative_adapter_same_contract(self):
        row = asdict(BASE)
        row["observed_at"] = NOW.isoformat()
        row["cancel_until"] = None
        self.assertEqual(
            Engine(NormalizedLocal([row])).search(REQ, NOW)["decisions"],
            self.run_offer()["decisions"],
        )

    def test_ranking_policy_can_change_without_provider_change(self):
        a = replace(BASE, amount="100", cancel_until=NOW + timedelta(days=1))
        b = replace(BASE, rate_id="b", amount="200", cancel_until=NOW + timedelta(days=2))
        provider = ListProvider([a, b])
        cheap = Engine(provider, ranking_policy="price").search(REQ, NOW)
        late = Engine(provider, ranking_policy="cancellation").search(REQ, NOW)
        self.assertEqual(next(iter(cheap["ranked_cohorts"].values()))[0].identity[3], "rate1")
        self.assertEqual(next(iter(late["ranked_cohorts"].values()))[0].identity[3], "b")

    def test_ranking_unknown_cancel_not_inferred(self):
        self.assertEqual(
            self.run_offer(ranking_policy="cancellation")["decisions"][0].status, "REVIEW"
        )

    def test_rank_invalid_policy(self):
        with self.assertRaises(InputError):
            Engine(ListProvider([]), ranking_policy="magic").search(REQ, NOW)

    def test_archive_adapter_36_rates(self):
        rows = json.loads((Path(__file__).parent / "fixtures/tourmind_36.json").read_text())
        req = Request("Dubai", "AE", "2026-10-26", "2026-10-28", (Guests(2),), "100000", "CNY")
        r = Engine(ArchivedTourMind(rows)).search(req, NOW)
        self.assertEqual(len(r["decisions"]), 36)
        self.assertFalse(r["issues"])
        self.assertTrue(
            all(d.status == "REVIEW" and d.evidence == "ARCHIVED_RESPONSE" for d in r["decisions"])
        )

    def test_random_arithmetic_2000(self):
        rng = random.Random(13)
        for i in range(2000):
            amount = Decimal(rng.randrange(0, 100000)) / 100
            req = replace(REQ, rooms=tuple(Guests(1) for _ in range(rng.randrange(1, 9))))
            o = replace(BASE, amount=str(amount), guests=req.rooms)
            self.assertEqual(Decimal(self.d(o, req).base_total), amount * 2 * len(req.rooms))


if __name__ == "__main__":
    unittest.main(verbosity=2)
