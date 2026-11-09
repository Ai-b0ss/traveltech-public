"""Two interchangeable local adapters. Neither accesses a live supplier."""

from datetime import datetime
from travel_core import Offer, Guests, SourceError


class ArchivedTourMind:
    def __init__(self, rows):
        self.rows = rows

    def search(self, request):
        offers = []
        for x in self.rows:
            # Captured query context: Dubai / UAE. No invented hotel amenities.
            if x["rooms_requested"] != 1 or x["children"] != 0:
                raise SourceError("UNSUPPORTED_CAPTURE_OCCUPANCY")
            fee = x.get("conditional_stripe_fee") or {}
            cancellation = x.get("cancellation") or {}
            deadline = cancellation.get("free_cancel_deadline")
            offers.append(
                Offer(
                    "TOURMIND",
                    x["hotel_id"],
                    x["room_id"],
                    x["rate_id"],
                    "Dubai",
                    "AE",
                    x["checkin"],
                    x["checkout"],
                    (Guests(x["adults"]),),
                    x["currency"],
                    x["stay_total"],
                    "FULL_STAY_ALL_ROOMS",
                    None,
                    "ARCHIVED_RESPONSE",
                    datetime.fromisoformat(x["observed_at"]),
                    x["source_file"] + "#" + x["rate_id"],
                    None,
                    datetime.fromisoformat(deadline) if deadline else None,
                    x.get("occupancy"),
                    str(fee["fee_amount"]) if "fee_amount" in fee else None,
                    "STRIPE" if "fee_amount" in fee else None,
                )
            )
        return offers


class NormalizedLocal:
    def __init__(self, rows):
        self.rows = rows

    def search(self, request):
        return [
            Offer(
                **{
                    **x,
                    "guests": tuple(
                        Guests(g["adults"], tuple(g.get("children", []))) for g in x["guests"]
                    ),
                    "observed_at": datetime.fromisoformat(x["observed_at"]),
                    "cancel_until": datetime.fromisoformat(x["cancel_until"])
                    if x.get("cancel_until")
                    else None,
                }
            )
            for x in self.rows
        ]
