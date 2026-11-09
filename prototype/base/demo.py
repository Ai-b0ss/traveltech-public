"""Command-line fixture demonstration. Synthetic data always remains labeled."""

import argparse, json
from pathlib import Path
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
from travel_core import *
from adapters import ArchivedTourMind


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", choices=["tourmind-archive", "synthetic"], default="tourmind-archive"
    )
    parser.add_argument("--russia-only", action="store_true")
    parser.add_argument("--rank", choices=["price", "cancellation"], default="price")
    args = parser.parse_args()
    now = datetime(
        2026, 10, 13, 11, 47, tzinfo=timezone.utc
    )  # Fixed replay clock, not a fresh observation.
    if args.source == "tourmind-archive":
        req = Request(
            "Dubai",
            "AE",
            "2026-10-26",
            "2026-10-28",
            (Guests(2),),
            "100000",
            "CNY",
            allowed_countries=("RU",) if args.russia_only else (),
        )
        provider = ArchivedTourMind(
            json.loads((Path(__file__).parent / "fixtures/tourmind_36.json").read_text())
        )
    else:
        req = Request(
            "Moscow",
            "RU",
            "2026-10-26",
            "2026-10-28",
            (Guests(2),),
            "10000",
            "RUB",
            allowed_countries=("RU",) if args.russia_only else (),
        )
        rows = [
            Offer(
                "SYNTHETIC",
                "demo_hotel_" + str(i),
                "demo_room",
                "demo_rate_" + str(i),
                "Moscow",
                "RU",
                req.checkin,
                req.checkout,
                req.rooms,
                "RUB",
                str(1000 + i * 300),
                "FULL_STAY_ALL_ROOMS",
                True,
                "SYNTHETIC",
                now,
                "synthetic:demo",
                cancel_until=now + timedelta(days=i + 1),
                capacity=2,
            )
            for i in range(5)
        ]
        provider = ListProvider(rows)
    try:
        result = Engine(provider, ranking_policy=args.rank).search(req, now)
    except InputError as e:
        print(
            json.dumps(
                {"status": "INPUT_ERROR", "reason": str(e), "inventory": "NOT_VERIFIED"},
                ensure_ascii=False,
            )
        )
        return 2
    result.update(
        replay_clock=now.isoformat(), source_mode=args.source, model_calls=0, network_calls=0
    )
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            default=lambda x: asdict(x) if hasattr(x, "__dataclass_fields__") else str(x),
        )
    )
    return 0 if result["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
