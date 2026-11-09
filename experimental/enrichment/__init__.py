"""Optional FX and POI enrichment. Remote calls stay off unless a caller opts in.

TripPlan integration is intentionally not wired here. See docs/enrichment/FX_AND_POI.md.
"""

from .fx import (
    FRANKFURTER_LIVE_ENABLED,
    MoneyConversion,
    RateObservation,
    convert_money,
    fetch_frankfurter_rate,
    map_money_line,
    verified_total,
)
from .poi import POI_LIVE_ENABLED, PoiFact, live_eiffel_smoke, nearby_pois

__all__ = [
    "FRANKFURTER_LIVE_ENABLED",
    "MoneyConversion",
    "POI_LIVE_ENABLED",
    "PoiFact",
    "RateObservation",
    "convert_money",
    "fetch_frankfurter_rate",
    "live_eiffel_smoke",
    "map_money_line",
    "nearby_pois",
    "verified_total",
]
