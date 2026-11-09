"""Focused tests for dynamic session-aware TripPlan assembler (experimental/trip_plan_session.py).

Verifies:
- Exact currency/cents and arithmetic preservation
- Source mode and provider tracking (LIVE_FIRST enforcement)
- Current revision, guests, dates, destination, origin, and budget scope
- Fail closed behavior on missing origin, missing budget scope, stale revision
- Standalone grounded offline demo cases
- End-to-end integration with CartSnapshot and MockOrderStore
- Schedule instant checking (local offsets, transfer before arrival)
"""

# These imports intentionally follow explicit experimental paths for standalone tests.
# ruff: noqa: E402

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experimental"))
sys.path.insert(0, str(ROOT / "prototype/base"))

from trip_plan_session import (
    SessionTripPlanAssembler,
    TripPlanSessionError,
    TripPlanContextUnknownError,
    StaleRevisionError,
    extract_session_context,
    build_session_trip_plan_status,
    create_grounded_dubai_case,
    create_grounded_kazan_case,
    create_offline_unpriced_paris_case,
    create_offline_stale_dates_case,
    handle_session_trip_plan_request,
)
from trip_plan import budget_status
from mock_cart import CartSnapshot, MockOrderStore


class SessionTripPlanAssemblerTests(unittest.TestCase):
    """Test suite for SessionTripPlanAssembler."""

    def test_grounded_dubai_case_exact_amounts_and_cart_snapshot(self):
        plan = create_grounded_dubai_case()
        self.assertEqual(plan["trip_id"], "session-grounded-dubai-demo-sid-12345678-rev0")
        self.assertEqual(plan["revision"], 0)
        self.assertEqual(plan["origin"], "Москва")
        self.assertEqual(plan["destination"], "Дубай")
        self.assertEqual(plan["start_date"], "2026-11-12")
        self.assertEqual(plan["end_date"], "2026-11-19")
        self.assertEqual(plan["currency"], "RUB")
        self.assertEqual(plan["travellers"], {"adults": 2, "children": [4]})
        self.assertEqual(plan["budget"], {"amount": "150000.00", "currency": "RUB", "scope": "whole_trip"})

        # Exact arithmetic: 81000 + 54000 + 5500 = 140500.00 RUB
        self.assertEqual(plan["hotel"]["amount"], "81000.00")
        self.assertEqual(plan["transport"]["amount"], "54000.00")
        self.assertEqual(plan["transfer"]["amount"], "5500.00")
        self.assertIsNone(plan["insurance"]["amount"])
        self.assertEqual(plan["insurance"]["fare_status"], "UNKNOWN")
        self.assertEqual(plan["total_amount"], "140500.00")

        # Budget fit evaluation
        self.assertTrue(budget_status(plan))

        # Compatibility with CartSnapshot and MockOrderStore
        with tempfile.TemporaryDirectory() as storage:
            store_path = Path(storage) / "mock_orders.sqlite"
            order_store = MockOrderStore(store_path)
            cart = CartSnapshot(plan, owner="test-owner-1", cart_id="cart-dubai-0", expected_revision=0)
            self.assertEqual(cart.revision, 0)
            saved_cart = order_store.save_cart(cart, expected_revision=0)
            self.assertEqual(saved_cart["cart_id"], "cart-dubai-0")

            order = order_store.process(
                owner="test-owner-1",
                cart_id="cart-dubai-0",
                expected_revision=0,
                idempotency_key="key-order-1",
            )
            self.assertEqual(order["status"], "DEMO_CREATED")
            self.assertEqual(order["payment_status"], "NOT_PROCESSED")
            self.assertEqual(order["booking_status"], "NOT_BOOKED")

    def test_grounded_kazan_hotel_only_budget(self):
        plan = create_grounded_kazan_case()
        self.assertEqual(plan["origin"], "Казань")
        self.assertEqual(plan["destination"], "Дубай")
        self.assertEqual(plan["budget"]["scope"], "hotel_only")
        self.assertEqual(plan["budget"]["amount"], "90000.00")
        # 81000 hotel <= 90000 limit -> True
        self.assertTrue(budget_status(plan))

    def test_fail_closed_on_missing_origin(self):
        session = {
            "sid": "session-no-origin-12345678",
            "revision": 0,
            "destination": "Сочи",
            "start_date": "2026-11-12",
            "end_date": "2026-11-15",
            "currency": "RUB",
            "budget": {"amount": "50000.00", "currency": "RUB", "scope": "whole_trip"},
            "travellers": {"adults": 1, "children": []},
        }
        # Origin completely missing
        with self.assertRaises(TripPlanContextUnknownError) as caught:
            SessionTripPlanAssembler.assemble(session)
        self.assertEqual(caught.exception.code, "ORIGIN_UNKNOWN")

        # Origin is placeholder '??????'
        with self.assertRaises(TripPlanContextUnknownError) as caught_placeholder:
            SessionTripPlanAssembler.assemble(session, origin="??????")
        self.assertEqual(caught_placeholder.exception.code, "ORIGIN_UNKNOWN")

        # Status inspection returns needs_input without throwing
        status = build_session_trip_plan_status(session)
        self.assertEqual(status["status"], "needs_input")
        self.assertIn("origin", status["missing_fields"])
        self.assertTrue(any("вылетаете" in q for q in status["questions"]))
        self.assertIsNone(status["plan"])

    def test_fail_closed_on_missing_budget_scope(self):
        session = {
            "sid": "session-no-scope-12345678",
            "revision": 0,
            "destination": "Сочи",
            "start_date": "2026-11-12",
            "end_date": "2026-11-15",
            "currency": "RUB",
            "origin": "Москва",
            # Budget specified as bare string, no scope supplied
            "budget": "50000.00",
            "travellers": {"adults": 1, "children": []},
        }
        with self.assertRaises(TripPlanContextUnknownError) as caught:
            SessionTripPlanAssembler.assemble(session)
        self.assertEqual(caught.exception.code, "BUDGET_SCOPE_UNKNOWN")

        status = build_session_trip_plan_status(session, origin="Москва")
        self.assertEqual(status["status"], "needs_input")
        self.assertIn("budget_scope", status["missing_fields"])
        self.assertTrue(any("бюджета" in q for q in status["questions"]))

    def test_explicit_no_budget_allowed_with_unspecified_scope(self):
        session = {
            "sid": "session-unbudgeted-12345678",
            "revision": 0,
            "destination": "Дубай",
            "start_date": "2026-11-12",
            "end_date": "2026-11-19",
            "currency": "RUB",
            "origin": "Москва",
            "budget": None,
            "travellers": {"adults": 2, "children": []},
        }
        plan = SessionTripPlanAssembler.assemble(session)
        self.assertIsNone(plan["budget"]["amount"])
        self.assertEqual(plan["budget"]["scope"], "unspecified")
        self.assertIsNone(budget_status(plan))

    def test_offline_unpriced_paris_case_live_first(self):
        """A01 synthetic MOCK example, with no captured Paris supplier quote.

        LIVE_FIRST guarantees: never invent flights, transfer/transport amount is null,
        total_amount is null, warnings include PRICE_UNKNOWN.
        """
        plan = create_offline_unpriced_paris_case()
        self.assertEqual(plan["origin"], "Москва")
        self.assertEqual(plan["destination"], "Париж")
        self.assertEqual(plan["hotel"]["amount"], "38000.00")
        self.assertIsNone(plan["transport"]["amount"])
        self.assertIsNone(plan["transfer"]["amount"])
        self.assertIsNone(plan["total_amount"])

        warning_codes = [w["code"] for w in plan["warnings"]]
        self.assertIn("PRICE_UNKNOWN", warning_codes)
        transport_warning = [w for w in plan["warnings"] if w["component"] == "transport"]
        self.assertTrue(transport_warning)

        # Whole-trip budget cannot be satisfied if total is unknown
        self.assertIsNone(budget_status(plan))

    def test_live_first_mode_rejects_mock_as_live(self):
        """Under live_mode=True, MOCK or ARCHIVE offers cannot be presented as current live fares."""
        session = {
            "sid": "live-session-12345678",
            "revision": 0,
            "destination": "Дубай",
            "start_date": "2026-11-12",
            "end_date": "2026-11-19",
            "currency": "RUB",
            "origin": "Москва",
            "budget": {"amount": "150000.00", "currency": "RUB", "scope": "whole_trip"},
            "travellers": {"adults": 2, "children": []},
        }
        mock_hotel = {
            "name": "Mock Hotel",
            "amount": "80000.00",
            "source_mode": "MOCK",
            "provider": "mock-hotel",
        }
        plan = SessionTripPlanAssembler.assemble(session, hotel=mock_hotel, live_mode=True)
        # Price is cleared because MOCK is not LIVE
        self.assertIsNone(plan["hotel"]["amount"])
        self.assertIsNone(plan["total_amount"])
        self.assertEqual(plan["hotel"]["fare_status"], "UNKNOWN")

    def test_stale_revision_rejected(self):
        session = {
            "sid": "stale-session-12345678",
            "revision": 2,
            "destination": "Дубай",
            "start_date": "2026-11-12",
            "end_date": "2026-11-19",
            "currency": "RUB",
            "origin": "Москва",
            "budget": {"amount": "150000.00", "currency": "RUB", "scope": "whole_trip"},
            "travellers": {"adults": 2, "children": []},
        }
        with self.assertRaises(StaleRevisionError):
            SessionTripPlanAssembler.assemble(session, expected_revision=1)

    def test_date_shift_invalidates_prices_to_null(self):
        original = create_grounded_dubai_case()
        shifted = create_offline_stale_dates_case(original, days=2)
        self.assertEqual(shifted["start_date"], "2026-11-14")
        self.assertEqual(shifted["end_date"], "2026-11-21")
        self.assertEqual(shifted["revision"], 1)
        self.assertIsNone(shifted["hotel"]["amount"])
        self.assertIsNone(shifted["transport"]["amount"])
        self.assertIsNone(shifted["transfer"]["amount"])
        self.assertIsNone(shifted["total_amount"])
        self.assertEqual(shifted["hotel"]["fare_status"], "STALE")
        self.assertIn("STALE_REVISION", [w["code"] for w in shifted["warnings"]])

    def test_schedule_warnings_instant_comparison(self):
        session = {
            "sid": "sched-session-12345678",
            "revision": 0,
            "destination": "Дубай",
            "start_date": "2026-11-12",
            "end_date": "2026-11-19",
            "currency": "RUB",
            "origin": "Москва",
            "budget": None,
            "travellers": {"adults": 1, "children": []},
        }
        # Checkin before arrival: arrives Nov 13 00:30, checkin is Nov 12
        transport = {
            "departure_at": "2026-11-12T18:00:00+03:00",
            "arrival_at": "2026-11-13T00:30:00+04:00",
            "amount": "30000.00",
            "source_mode": "MOCK",
            "provider": "mock-air",
        }
        # Transfer before arrival: pickup at 00:15, arrival at 00:30
        transfer = {
            "fare_scope": "one_way_arrival",
            "pickup_at": "2026-11-13T00:15:00+04:00",
            "amount": "2000.00",
            "source_mode": "MOCK",
            "provider": "mock-transfer",
        }
        hotel = {
            "checkin": "2026-11-12",
            "checkout": "2026-11-19",
            "amount": "40000.00",
            "source_mode": "MOCK",
            "provider": "mock-hotel",
        }
        plan = SessionTripPlanAssembler.assemble(
            session,
            hotel=hotel,
            transport=transport,
            transfer=transfer,
        )
        codes = [w["code"] for w in plan["warnings"]]
        self.assertIn("CHECKIN_BEFORE_ARRIVAL", codes)
        self.assertIn("TRANSFER_BEFORE_ARRIVAL", codes)

    def test_mixed_currency_fails_closed(self):
        session = {
            "sid": "mixed-curr-12345678",
            "revision": 0,
            "destination": "Дубай",
            "start_date": "2026-11-12",
            "end_date": "2026-11-19",
            "currency": "RUB",
            "origin": "Москва",
            "budget": {"amount": "1000.00", "currency": "USD", "scope": "whole_trip"},
            "travellers": {"adults": 1, "children": []},
        }
        with self.assertRaises(TripPlanSessionError) as caught:
            SessionTripPlanAssembler.assemble(session)
        self.assertEqual(caught.exception.code, "MIXED_CURRENCY")

    def test_multi_room_guest_aggregation(self):
        session = {
            "sid": "rooms-session-12345678",
            "revision": 0,
            "city": "Дубай",
            "checkin": "2026-11-12",
            "checkout": "2026-11-19",
            "currency": "RUB",
            "origin": "Москва",
            "budget": None,
            "rooms": [
                {"adults": 2, "children": [4]},
                {"adults": 1, "children": [7]},
            ],
        }
        context = extract_session_context(session)
        self.assertEqual(context.adults, 3)
        self.assertEqual(context.children, (4, 7))

    def test_handle_session_trip_plan_rejects_untrusted_db_stub(self):
        class MockDB:
            def execute(self, query, params):
                class Cursor:
                    def fetchone(self):
                        if "session_owners" in query:
                            return ("owner-1",)
                        return None
                return Cursor()

        class MockStore:
            def __init__(self):
                self.db = MockDB()

            def get_session(self, sid):
                return {
                    "schema": 1,
                    "state": {
                        "revision": 0,
                        "request": {
                            "city": "Dubai",
                            "checkin": "2026-11-12",
                            "checkout": "2026-11-19",
                            "currency": "RUB",
                            "origin": "Москва",
                            "budget": {"amount": "150000.00", "currency": "RUB", "scope": "whole_trip"},
                            "rooms": [{"adults": 2, "children": [4]}],
                        },
                    },
                }

        store = MockStore()
        code, resp = handle_session_trip_plan_request(
            store,
            owner="owner-1",
            sid="0123456789abcdef0123456789abcdef",
            origin="Москва",
            budget_scope="whole_trip",
        )
        self.assertEqual(code, 403)
        self.assertEqual(resp["error"], "OWNER_AUTHORITY_UNSUPPORTED")

        # Wrong owner gives 403
        code_forbidden, resp_forbidden = handle_session_trip_plan_request(
            store,
            owner="intruder",
            sid="0123456789abcdef0123456789abcdef",
        )
        self.assertEqual(code_forbidden, 403)
        self.assertEqual(resp_forbidden["error"], "OWNER_AUTHORITY_UNSUPPORTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
