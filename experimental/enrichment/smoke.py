"""Four or five public GETs. Unit tests do not import this as a fixture oracle."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import experimental.enrichment.fx as fx
import experimental.enrichment.http_get as http_get
import experimental.enrichment.poi as poi
from experimental.enrichment.errors import EnrichmentError

BUDGET = 5


def collect() -> dict:
    calls = []
    original = http_get.http_get

    def wrapped(url, **kwargs):
        calls.append(url)
        if len(calls) > BUDGET:
            raise EnrichmentError("SMOKE_CALL_BUDGET")
        return original(url, **kwargs)

    fx.http_get = wrapped
    poi.http_get = wrapped
    report = {
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "not_a_unit_test_oracle": True,
        "checkout_performed": False,
        "device_location_used": False,
        "control_point": {"lat": "48.8584", "lon": "2.2945", "name": "Eiffel Tower published control point"},
        "calls": [],
    }
    try:
        try:
            observation = fx.fetch_frankfurter_rate("EUR", "USD", allow_remote=True)
            report["calls"].append(
                {
                    "name": "frankfurter_eur_usd",
                    "url": observation.source_url,
                    "mode": observation.mode,
                    "date": observation.as_of.isoformat(),
                    "base": observation.base,
                    "quote": observation.quote,
                    "rate": format(observation.rate, "f"),
                    "checkout_rate": observation.checkout_rate,
                    "note": "Point-in-time ECB reference via Frankfurter. Not a unit-test oracle and not a checkout rate.",
                }
            )
        except EnrichmentError as exc:
            report["calls"].append({"name": "frankfurter_eur_usd", "error": exc.code})
        for name, source in (
            ("wikidata_geosearch", "wikidata"),
            ("wikipedia_geosearch", "wikipedia"),
            ("wikivoyage_geosearch", "wikivoyage"),
        ):
            try:
                facts = poi.live_eiffel_smoke(source, allow_remote=True, radius_m=850, limit=12)
                report["calls"].append(
                    {
                        "name": name,
                        "url": facts[0].source_url,
                        "count": len(facts),
                        "rights": facts[0].rights,
                        "items": [
                            {
                                "title": fact.title,
                                "qid": fact.qid,
                                "lat": format(fact.latitude, "f"),
                                "lon": format(fact.longitude, "f"),
                                "distance_m": format(fact.distance_m, "f"),
                                "ticket_price": fact.ticket_price,
                                "opening_hours": fact.opening_hours,
                                "bookable": fact.bookable,
                            }
                            for fact in facts[:12]
                        ],
                    }
                )
            except EnrichmentError as exc:
                report["calls"].append({"name": name, "error": exc.code})
    finally:
        fx.http_get = original
        poi.http_get = original
    report["http_get_count"] = len(calls)
    return report


if __name__ == "__main__":
    from pathlib import Path

    report = collect()
    target = Path(__file__).resolve().parents[2] / "docs" / "enrichment" / "live_smoke_20261025.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote", target.name, "gets", report["http_get_count"])
