"""Fixed synthetic hotel API implementing the existing Provider.search contract.

IDs, prices and properties below are test inputs, never current hotel facts.
No network, credentials, dynamic pricing or live-inventory claim.
"""

from dataclasses import replace
from datetime import datetime, timezone
from travel_core import Offer, Guests, SourceError


class DeterministicHotelAPI:
    def __init__(self, mode="normal"):
        self.mode = mode
        self.calls = []
        quote = Offer(
            "SYNTHETIC",
            "FIXTURE-H1",
            "FIXTURE-R1",
            "FIXTURE-T1",
            "Сочи",
            "RU",
            "2026-10-26",
            "2026-10-28",
            (Guests(2),),
            "RUB",
            "6000",
            "FULL_STAY_ALL_ROOMS",
            True,
            "SYNTHETIC",
            datetime(2026, 10, 14, tzinfo=timezone.utc),
            "fixture://hotel-api/2026-10-14/H1/R1/T1",
            breakfast=True,
            capacity=2,
        )
        self.rows = [
            quote,
            replace(
                quote,
                hotel_id="FIXTURE-H2",
                room_id="FIXTURE-R2",
                rate_id="FIXTURE-T2",
                amount="4000",
                breakfast=False,
                source_ref="fixture://hotel-api/2026-10-14/H2/R2/T2",
            ),
            replace(
                quote,
                hotel_id="FIXTURE-H3",
                room_id="FIXTURE-R3",
                rate_id="FIXTURE-T3",
                amount="3000",
                taxes_known=None,
                capacity=None,
                source_ref="fixture://hotel-api/2026-10-14/H3/R3/T3",
            ),
        ]

    def search(self, request):
        request.validate()
        self.calls.append(request)
        if self.mode == "timeout":
            raise TimeoutError("FIXTURE_TOOL_TIMEOUT_UNKNOWN_OUTCOME")
        if self.mode == "malformed":
            raise SourceError("FIXTURE_MALFORMED_API_RESPONSE")
        if self.mode == "empty":
            return []
        return list(self.rows)
