"""Reduce oversized live supplier snapshots without inventing hotel evidence.

The full supplier response stays in the supplier's own read-only journal.
Only a bounded, source-backed selection is embedded into a trip Preview.
"""
from __future__ import annotations

from provider_transport import canonical
from .offer_shortlist import shortlist

MAX_PREVIEW_BYTES = 4_000_000
MAX_PREVIEW_OFFERS = 48


def compact_live_source(source, request, preference_text="", byte_budget=MAX_PREVIEW_BYTES):
    if not isinstance(source, dict) or not isinstance(source.get("offers"), list):
        return source
    if len(canonical(source).encode("utf-8")) <= byte_budget:
        return source

    originals = source["offers"]
    from provider_transport import Failure
    if not all(isinstance(o, dict) for o in originals):
        raise Failure("LIVE_OFFER_INVALID")
    candidates = [
        dict(
            o,
            _key=str(i),
            _price=o.get("total"),
            _currency=o.get("currency"),
            _reasons=o.get("reasons") or [],
            fx=source.get("fx"),
        )
        for i, o in enumerate(originals)
    ]
    limits = (48, 32, 24, 16, 12, 8, 4, 2, 1)
    for limit in limits:
        chosen = shortlist(
            candidates,
            request,
            max_count=min(limit, len(candidates)) if candidates else 1,
            preferences_text=preference_text,
        ) if candidates else []
        selection = [originals[int(item["_key"])] for item in chosen]
        coverage = dict(source.get("coverage") or {})
        coverage.update({
            "observed_offer_count": len(originals),
            "preview_offer_count": len(selection),
            "preview_rate_selection_truncated": len(originals) > len(selection),
            "complete_city": False,
        })
        bounded = {
            **source,
            "offers": selection,
            "coverage": coverage,
            "preview_selection_policy": "VERIFIED_FACTS_DIVERSE_HOTELS_CURRENCY_SAFE",
        }
        if len(canonical(bounded).encode("utf-8")) <= byte_budget:
            return bounded
    raise Failure("LIVE_SOURCE_TOO_LARGE_AFTER_BOUNDED_SELECTION")
