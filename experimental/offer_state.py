"""One durable supplier observation bound to an exact trip request.

This is read-only evidence, never a reservation or an inventory guarantee.
The existing travel_core evaluator applies common constraints to both sources.
"""

import copy, hashlib, json
from datetime import datetime, timezone, timedelta
from travel_core import Offer, Guests, Engine, ListProvider, InputError
from provider_transport import canonical

MAX_BYTES = 8_000_000


def bind(request, result):
    if not isinstance(result, dict) or not isinstance(result.get("offers"), list):
        raise InputError("LIVE_SOURCE_INVALID")
    expected = json.loads(canonical(request))
    if canonical(result.get("request")) != canonical(expected):
        raise InputError("LIVE_SOURCE_CONTEXT_MISMATCH")
    raw = canonical(result)
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise InputError("LIVE_SOURCE_TOO_LARGE")
    value = json.loads(raw)
    value["availability_verified"] = False
    value["booking_performed"] = False
    return value


def project(request, result, now=None):
    now = now or datetime.now(timezone.utc)
    data = bind(request, result)
    cards = []
    seen = {}
    try:
        stamp = datetime.fromisoformat(data["observed_at"])
        stale = stamp.tzinfo is None or not timedelta(0) <= now - stamp <= timedelta(seconds=300)
    except (KeyError, TypeError, ValueError):
        stale = True
    for original in data["offers"]:
        if not isinstance(original, dict):
            raise InputError("LIVE_OFFER_INVALID")
        o = copy.deepcopy(original)
        provider = o.get("source") or "TOURMIND_LIVE_RATE"
        if not isinstance(provider, str) or not 1 <= len(provider) <= 128:
            raise InputError("LIVE_SOURCE_INVALID")
        ids = [o.get(k) for k in ("hotel_id", "room_id", "rate_code")]
        if any(not isinstance(x, str) or not 1 <= len(x) <= 256 for x in ids):
            raise InputError("LIVE_OFFER_IDENTITY_INVALID")
        key = hashlib.sha256(
            canonical({"provider": provider, "identity": ids, "request": request}).encode("utf-8")
        ).hexdigest()
        # Equal identity with contradictory contents is never silently merged.
        if key in seen:
            prior = seen[key]
            if canonical(prior["observation"]) != canonical(original):
                prior["card"]["status"] = "REJECT"
                prior["card"]["reasons"].append("DUPLICATE_CONFLICT")
                prior["card"]["reason_texts"].append("Противоречивые данные одного тарифа")
            continue
        reasons = list(dict.fromkeys(o.get("reasons") or []))
        status = "REJECT" if o.get("status") == "REJECT" else "REVIEW"
        try:
            occupancy = tuple(
                Guests(r["adults"], tuple(r.get("children", []))) for r in o["occupancy"]
            )
            deadline = (o.get("cancellation") or {}).get("free_cancel_deadline")
            offer = Offer(
                provider,
                *ids,
                o.get("city", request["city"]),
                o.get("country", request["country"]),
                o["check_in"],
                o["check_out"],
                occupancy,
                o["currency"],
                o.get("total"),
                o.get("price_basis", "UNKNOWN"),
                o.get("taxes_known"),
                "PROVIDER_RESPONSE",
                datetime.fromisoformat(o["observed_at"]),
                "supplier-observation://" + key,
                breakfast=o.get("breakfast"),
                capacity=o.get("capacity"),
                cancel_until=datetime.fromisoformat(deadline) if deadline else None,
            )
            from app import request_from

            checked = Engine(ListProvider([offer]), max_age=timedelta(seconds=300)).search(
                request_from(request), now
            )
            if not checked.get("decisions"):
                raise InputError("CANONICAL_SOURCE_REJECTED")
            d = checked["decisions"][0]
            reasons = list(dict.fromkeys(reasons + list(d.reasons)))
            if d.status == "REJECT":
                status = "REJECT"
        except (ValueError, KeyError, TypeError):
            reasons = list(dict.fromkeys(reasons + ["SOURCE_FIELDS_UNVERIFIED"]))
        if stale:
            reasons = list(dict.fromkeys(reasons + ["STALE_OR_FUTURE_SOURCE"]))
        reasons = list(
            dict.fromkeys(reasons + ["AVAILABILITY_NOT_CHECKED", "FINAL_PAYABLE_PRICE_UNVERIFIED"])
        )
        known_fee = (o.get("supplier_facts") or {}).get("known_excluded_total") or {}
        try:
            from live_data import number

            if (
                known_fee.get("currency") == o.get("currency") == request["currency"]
                and o.get("total") is not None
                and number(o["total"]) + number(known_fee.get("amount")) > number(request["budget"])
            ):
                reasons.append("KNOWN_TOTAL_OVER_BUDGET")
                status = "REJECT"
        except (ValueError, TypeError):
            pass
        if (
            request.get("free_cancel_until")
            and (o.get("cancellation") or {}).get("effective_non_refundable") is True
        ):
            reasons.append("NON_REFUNDABLE")
            status = "REJECT"
        from hotel_requirements import check, reason_text

        gated = check(dict(o, status=status, reasons=reasons), data.get("hotel_requirements") or {})
        reasons = gated["reasons"]
        status = gated["status"]
        identity = [
            provider,
            *ids,
            o.get("check_in"),
            o.get("check_out"),
            o.get("occupancy"),
            o.get("currency"),
            "PROVIDER_RESPONSE",
        ]
        recheck = copy.deepcopy(o.get("availability_check"))
        if isinstance(recheck, dict):
            try:
                checked_at = datetime.fromisoformat(recheck["observed_at"])
                fresh = checked_at.tzinfo is not None and timedelta(
                    0
                ) <= now - checked_at <= timedelta(seconds=300)
            except (TypeError, ValueError, KeyError):
                fresh = False
            if not fresh or o.get("quote_change"):
                recheck["availability_verified"] = False
            recheck["stale"] = not fresh
        card = {
            "offer_id": key,
            "name": o.get("hotel_name") or "Отель " + ids[0],
            "room_name": o.get("room_name") or "Номер " + ids[1],
            "identity": identity,
            "status": status,
            "reasons": reasons,
            "reason_texts": reasons.copy(),
            "base_total": o.get("total"),
            "currency": o.get("currency"),
            "payable_for_method": None,
            "fee_method": None,
            "evidence": "PROVIDER_RESPONSE",
            "source_ref": "supplier-observation://" + key,
            "inventory": "NOT_VERIFIED",
            "metadata": {"context_binding": "REQUEST_BOUND_NOT_PROVIDER_VERIFIED"},
            "source_kind": o.get("source_kind", data.get("source_kind", "PROVIDER_RATE")),
            "supplier_facts": o.get("supplier_facts") or {},
            "hotel_detail_evidence": o.get("hotel_detail_evidence"),
            "cost_evidence": o.get("cost_evidence"),
            "hard_requirement_checks": gated["hard_requirement_checks"],
            "hard_requirements_satisfied": gated["hard_requirements_satisfied"],
            "recommendable": gated["recommendable"],
            "availability_check": recheck,
            "quote_change": o.get("quote_change"),
            "quote_checkin": o.get("check_in"),
            "quote_checkout": o.get("check_out"),
            "price_basis": "За всех гостей и всё проживание"
            if o.get("price_basis") == "FULL_STAY_ALL_ROOMS"
            else "Основа цены не подтверждена",
            "breakfast": o.get("breakfast"),
            "capacity": o.get("capacity"),
            "taxes_known": o.get("taxes_known"),
            "observed_at_or_date": o.get("observed_at"),
            "observation_precision": "CAPTURE_TIMESTAMP",
            "cancel_until": (o.get("cancellation") or {}).get("free_cancel_deadline"),
        }
        from app import REASONS

        labels = {
            **REASONS,
            "AVAILABILITY_NOT_CHECKED": "Наличие не проверено",
            "FINAL_PAYABLE_PRICE_UNVERIFIED": "Окончательный счёт не подтверждён",
            "SOURCE_FIELDS_UNVERIFIED": "Часть полей источника не подтверждена",
            "DUPLICATE_CONFLICT": "Противоречивые данные одного тарифа",
            "KNOWN_TOTAL_OVER_BUDGET": "Цена с известными доплатами превышает бюджет",
            "NON_REFUNDABLE": "Тариф не допускает требуемую бесплатную отмену",
            "SANDBOX_NOT_LIVE_INVENTORY": "Тестовая среда поставщика: реальные цена и наличие не подтверждены",
            "SUPPLIER_OCCUPANCY_UNVERIFIED": "Поставщик не подтвердил полный состав гостей",
            "SUPPLIER_DATES_REQUEST_BOUND": "Даты привязаны к запросу; независимое подтверждение поставщика отсутствует",
            "EXCLUDED_FEES_UNPRICED": "Есть доплаты без подтверждённой суммы в валюте тарифа",
        }
        card["reason_texts"] = [labels.get(r, reason_text(r)) for r in reasons]
        cards.append(card)
        seen[key] = {"card": card, "observation": original}
    data.update(
        snapshot_stale=stale,
        snapshot_is_booking=False,
        availability_verified=False,
        booking_performed=False,
    )
    return cards, data
