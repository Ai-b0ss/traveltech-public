"""Local offline experiment. Reuses the baseline; no supplier/model network code."""

from pathlib import Path
import sys, json, hashlib, os, threading, uuid, argparse, time
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import socket
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "prototype"), str(ROOT / "prototype/base")]
from travel_core import (
    Request,
    Guests,
    Offer,
    Engine,
    ListProvider,
    InputError,
    SourceError,
    safe_json,
)
from session import Session, State, Conflict
from current_adapter import CurrentCapture
from model_interface import dispatch
from reformboss_client import ReformbossClient, ModelError
from natural_language import NaturalLanguage
from oneapi_client import OneAPIClient
from proposal_store import ProposalStore
from ai_router import OfflineClient, ShadowRouter, AdapterError
from pet_preferences import project as pet_project, archived_policy


class ExclusiveHTTPServer(ThreadingHTTPServer):
    # Several browsers open parallel asset/config connections on startup.
    request_queue_size = 128
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


SCENARIOS = {
    "synthetic": "Синтетический нормальный пример",
    "dubai": "Архив TourMind · Dubai",
    "istanbul": "Архив TourMind · Istanbul",
    "ru": "Архив Travel Search RU · Сочи",
    "error": "Тест ошибки источника",
    "empty": "Тест пустой выдачи",
}
REASONS = {
    "PET_POLICY_UNVERIFIED": "Размещение животного не подтверждено",
    "PET_POLICY_INCOMPATIBLE": "В архиве отеля явно запрещены животные",
    "PET_FEES_UNKNOWN": "Доплата за питомца неизвестна",
    "PROPERTY_FEES_REQUIRE_REVIEW": "В отеле могут быть отдельные платежи: проверьте сумму и условия",
    "SUPPLIER_FACT_CONFLICT": "Сведения поставщика об удобствах противоречат друг другу",
    "EXCLUDED_FEES_UNPRICED": "Отдельные сборы не удалось включить в расчёт",
    "BREAKFAST_NOT_INCLUDED": "Завтрак не включён в сохранённый тариф",
    "BREAKFAST_UNKNOWN": "Наличие завтрака не подтверждено",
    "BASE_PRICE_OVER_BUDGET": "Базовая цена превышает бюджет",
    "CAPACITY_UNKNOWN": "Вместимость номера неизвестна",
    "TAX_COVERAGE_UNVERIFIED": "Полнота налогов и сборов не подтверждена",
    "STALE_OR_FUTURE_SOURCE": "Актуальность архивного наблюдения не подтверждена",
    "PAYMENT_METHOD_COST_UNVERIFIED": "Стоимость выбранного способа оплаты не подтверждена",
    "HOTEL_ID_UNVERIFIED": "ID отеля не подтверждён; используется локальный ID записи",
    "ROOM_ID_UNVERIFIED": "ID номера не подтверждён; тарифы не объединяются",
    "ROOM_ALLOCATION_UNVERIFIED": "Распределение гостей по номерам не подтверждено",
    "DETAILS_NOT_CAPTURED": "Детали этого результата не сохранены",
    "ROOM_TEXT_ENCODING_LOSS": "В архиве потеряна кодировка названия номера",
    "QUOTE_FRESHNESS_UNVERIFIED": "Актуальность краткоживущего предложения не подтверждена",
    "LOCATION_UNVERIFIED": "Точное местоположение отеля не подтверждено",
    "ROOM_NAME_OCCUPANCY_REQUIRES_REVIEW": "Название одноместного номера требует проверки для двух гостей",
    "SOURCE_ERROR": "Ошибка источника; это не пустой результат",
    "REQUEST_CONTEXT_MISMATCH": "Архивный тариф относится к другим датам или гостям; цена не применима к этой поездке",
    "FX_NOT_AVAILABLE": "Нет подтверждённого курса валют",
    "CAPACITY_EXCEEDED": "Подтверждённая вместимость недостаточна",
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def jsonable(value):
    return json.loads(
        json.dumps(
            value,
            default=lambda x: asdict(x)
            if hasattr(x, "__dataclass_fields__")
            else x.isoformat()
            if isinstance(x, datetime)
            else list(x)
            if isinstance(x, tuple)
            else str(x),
            ensure_ascii=False,
        )
    )


def request_from(data):
    data = dict(data)
    data["rooms"] = tuple(Guests(r["adults"], tuple(r.get("children", []))) for r in data["rooms"])
    data["allowed_countries"] = tuple(data.get("allowed_countries", []))
    if data.get("free_cancel_until"):
        data["free_cancel_until"] = datetime.fromisoformat(data["free_cancel_until"])
    request = Request(**data)
    request.validate()
    return request


class ArchiveTourMind:
    def __init__(self, city, country):
        base = ROOT / "prototype/results"
        self.rates = base / (city.lower() + "_sample_rates.json")
        self.details = base / (city.lower() + "_sample_detail.json")
        self.adapter = CurrentCapture(read(self.rates), read(self.details), city, country)

    def search(self, request):
        # CurrentCapture was named when the responses were taken; replay is always ARCHIVED here.
        self.last_offers = [
            replace(
                o,
                evidence="ARCHIVED_RESPONSE",
                source_ref=str(self.rates.relative_to(ROOT)).replace("\\", "/") + "#" + o.rate_id,
            )
            for o in self.adapter.search(request)
        ]
        return self.last_offers


class ArchiveRU:
    def __init__(self):
        base = ROOT / "research/working_snapshots/stage6"
        self.search_path = base / "public_hotel_probe.json"
        self.detail_path = base / "public_detail_probe.json"
        self.capture = read(self.search_path)
        self.details = read(self.detail_path)
        if self.capture.get("summary", {}).get("status") != "SEARCH_RESPONSE_RECEIVED":
            raise SourceError("RU_CAPTURE_FAILED")
        self.rows = self.capture["summary"]["result"]["offers"]
        self.detail = self.details.get("summary", {}).get("result", {})
        # The date is known, time is absent: midnight is a conservative LOWER BOUND, never a measured timestamp.
        self.lower_bound = datetime.fromisoformat(self.capture["date"]).replace(tzinfo=timezone.utc)
        self.metadata = {}

    def search(self, request):
        criteria = self.capture["request_criteria"]
        offers = []
        self.metadata = {}
        for row in self.rows:
            if (
                row.get("check_in") != criteria["date_from"]
                or row.get("nights") != criteria["nights_min"]
                or criteria["nights_min"] != criteria["nights_max"]
            ):
                raise SourceError("RU_CAPTURE_CONTEXT_MISMATCH")
            if criteria.get("adults") != 2 or row.get("country") != "Россия":
                raise SourceError("RU_CAPTURE_PARTY_OR_COUNTRY_MISMATCH")
            identifier = row["offer_id"]
            digest = hashlib.sha256(identifier.encode()).hexdigest()[:20]
            detail = self.detail if self.detail.get("offer_id") == identifier else None
            if detail and (
                detail.get("price") != row.get("price")
                or detail.get("currency") != row.get("currency")
            ):
                raise SourceError("RU_DETAIL_PRICE_MISMATCH")
            amount = row.get("price")
            if type(amount) is not int or amount < 0 or row.get("currency") != "RUB":
                raise SourceError("RU_PRICE_CONTRACT_INVALID")
            # Source response explicitly says price is all tourists/full stay. This does not establish tax coverage.
            basis_note = "Цена — за всех туристов и всё проживание."
            if not any(
                note.startswith(basis_note)
                for note in self.capture["summary"]["result"].get("notes", [])
            ):
                raise SourceError("RU_PRICE_BASIS_NOT_DOCUMENTED")
            checkout = (
                (datetime.fromisoformat(row["check_in"]) + timedelta(days=row["nights"]))
                .date()
                .isoformat()
            )
            ref = str(self.search_path.relative_to(ROOT)).replace("\\", "/") + "#" + identifier
            offer = Offer(
                "TRAVEL_SEARCH_RU/LEVELTRAVEL",
                "archive-hotel:" + digest,
                "unverified-room:" + digest,
                identifier,
                "Сочи",
                "RU",
                row["check_in"],
                checkout,
                (Guests(criteria["adults"]),),
                "RUB",
                str(amount),
                "FULL_STAY_ALL_ROOMS",
                None,
                "ARCHIVED_RESPONSE",
                self.lower_bound,
                ref,
                breakfast=False if row.get("meal") == "RO" else None,
                capacity=None,
            )
            issues = [
                "HOTEL_ID_UNVERIFIED",
                "ROOM_ID_UNVERIFIED",
                "ROOM_ALLOCATION_UNVERIFIED",
                "QUOTE_FRESHNESS_UNVERIFIED",
                "LOCATION_UNVERIFIED",
            ]
            room_text = (
                detail.get("rooms", [{}])[0].get("name") if detail and detail.get("rooms") else None
            )
            if not detail:
                issues.append("DETAILS_NOT_CAPTURED")
            if room_text and "\ufffd" in room_text:
                issues.append("ROOM_TEXT_ENCODING_LOSS")
            if room_text and "одномест" in room_text.casefold():
                issues.append("ROOM_NAME_OCCUPANCY_REQUIRES_REVIEW")
            self.metadata[identifier] = {
                "name": row["hotel"],
                "rating": row.get("rating"),
                "room_name": room_text
                if room_text and "\ufffd" not in room_text
                else "Название номера не подтверждено",
                "extra_reasons": issues,
                "observation_precision": "DATE_ONLY_LOWER_BOUND",
                "captured_date": self.capture["date"],
                "source_sha256": sha(self.search_path),
                "detail_sha256": sha(self.detail_path) if detail else None,
                "identity_kind": "LOCAL_ARCHIVE_RECORD_NOT_PROVIDER_HOTEL_OR_ROOM_ID",
                "source_request": criteria,
                "checkout_derivation": "check_in + nights; no explicit supplier checkout field",
                "location_binding": "SOURCE_SEARCH_REGION_ONLY",
                "price_basis": "Source response notes: all tourists, full stay; not independent complete bill verification",
            }
            offers.append(offer)
        self.last_offers = offers
        return offers


def card_facts(provider, identity):
    offer = next(
        (
            o
            for o in getattr(provider, "last_offers", getattr(provider, "offers", []))
            if o.identity == identity
        ),
        None,
    )
    if offer is None:
        return {}
    return {
        **archived_policy(provider, identity),
        "breakfast": offer.breakfast,
        "capacity": offer.capacity,
        "taxes_known": offer.taxes_known,
        "price_basis": "За всех гостей и всё проживание"
        if offer.basis == "FULL_STAY_ALL_ROOMS"
        else "За номер за ночь × ночи × номера",
        "raw_amount": offer.amount,
        "cancel_until": offer.cancel_until.isoformat() if offer.cancel_until else None,
    }


class BrokenProvider:
    def search(self, request):
        raise TimeoutError("SYNTHETIC_SOURCE_TIMEOUT")


def scenario(name, now):
    if name not in SCENARIOS:
        raise InputError("UNKNOWN_SCENARIO")
    if name in ("dubai", "istanbul"):
        city, country = ("Dubai", "AE") if name == "dubai" else ("Istanbul", "TR")
        provider = ArchiveTourMind(city, country)
        p = read(provider.rates)["request"]
        req = Request(
            city,
            country,
            p["check_in_date"],
            p["check_out_date"],
            tuple(
                Guests(p["adults"], tuple(p.get("children_ages", [])))
                for _ in range(p["room_count"])
            ),
            "100000",
            "CNY",
        )
        return req, provider
    if name == "ru":
        return Request(
            "Сочи", "RU", "2026-10-26", "2026-10-28", (Guests(2),), "30000", "RUB"
        ), ArchiveRU()
    req = Request("Тестовый город", "RU", "2026-10-26", "2026-10-28", (Guests(2),), "10000", "RUB")
    if name == "error":
        return req, BrokenProvider()
    if name == "empty":
        return req, ListProvider([])
    offer = Offer(
        "SYNTHETIC",
        "TEST-H1",
        "TEST-R1",
        "TEST-T1",
        req.city,
        req.country,
        req.checkin,
        req.checkout,
        req.rooms,
        "RUB",
        "3000",
        "PER_ROOM_PER_NIGHT",
        True,
        "SYNTHETIC",
        now,
        "fixture://synthetic/normal",
        capacity=2,
    )
    return req, ListProvider(
        [
            offer,
            replace(offer, rate_id="TEST-T2", amount="3500"),
            replace(
                offer,
                hotel_id="TEST-H2",
                room_id="TEST-R2",
                rate_id="TEST-T3",
                amount="2000",
                taxes_known=None,
                capacity=None,
            ),
        ]
    )


class Application:
    def __init__(self, storage, model_client=None):
        self.storage = Path(storage)
        self.storage.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.store = ProposalStore(self.storage / "state.sqlite")
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS offer_observations(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.router = ShadowRouter()
        self.model_client = model_client or OfflineClient()
        self.live_enabled = False  # Can only be enabled by the opt-in synthetic launcher.
        self.language = NaturalLanguage(self, self.model_client)
        self.pending_previews = {}

    def start_draft(self, draft_id):
        # Reserve a durable, monotonically increasing sequence BEFORE network inference.
        # A reopened process must not reuse 1 after a previous draft reached 2+.
        self._path(draft_id)
        with self.lock, self.store.transaction():
            self.store.db.execute(
                "INSERT OR IGNORE INTO draft_latest_seq(id,seq) "
                "VALUES (?,COALESCE((SELECT seq FROM draft_groups WHERE id=?),0))",
                (draft_id, draft_id),
            )
            self.store.db.execute("UPDATE draft_latest_seq SET seq=seq+1 WHERE id=?", (draft_id,))
            return self.store.db.execute(
                "SELECT seq FROM draft_latest_seq WHERE id=?", (draft_id,)
            ).fetchone()[0]

    def close(self):
        self.store.db.close()

    def preview_proposal(
        self,
        sid,
        patch=None,
        revision=None,
        action="budget",
        additional_preferences=None,
        source_result=None,
    ):
        if additional_preferences is None:
            additional_preferences = []
        if (
            type(additional_preferences) is not list
            or len(additional_preferences) > 15
            or any(type(x) is not str or not 1 <= len(x) <= 1500 for x in additional_preferences)
        ):
            raise InputError("ADDITIONAL_PREFERENCES_INVALID")
        with self.lock, self.store.transaction():
            name, s = self._load(sid)
            if type(revision) is not int or revision != s.state.revision:
                raise Conflict("STALE_REVISION")
            now = datetime.now(timezone.utc)
            from refinement import normalize

            prospective_req = normalize(s.state.request, patch)
            prospective_req.validate()
            body = self.store.get_session(sid)
            if (
                source_result is not None
                or body.get("source_mode") == "live"
                or name == "live_dynamic"
            ):
                from offer_state import project

                cards, source = (
                    project(jsonable(prospective_req), source_result, now)
                    if source_result is not None
                    else ([], None)
                )
                if "city" in patch:
                    from city_names import canonical_city

                    name = canonical_city(prospective_req.city, prospective_req.country)[2]
                changes = {
                    k: {
                        "before": jsonable(getattr(s.state.request, k)),
                        "after": jsonable(getattr(prospective_req, k)),
                    }
                    for k in patch
                    if getattr(s.state.request, k) != getattr(prospective_req, k)
                }
                return self._create_proposal(
                    sid,
                    s.state.revision,
                    action,
                    {
                        "patch": patch,
                        "changes": changes,
                        "scenario": name,
                        "prospective_request": jsonable(prospective_req),
                        "prospective_cards": cards,
                        "source_mode": "live",
                        "source_result": source,
                        "unchanged_fields": [
                            k for k in jsonable(prospective_req) if k not in changes
                        ],
                        "summary": "Изменение условий поездки",
                        "status_impact": "Наблюдение поставщика; наличие и окончательная цена не подтверждены"
                        if source
                        else "Условия изменены: требуется новый поиск",
                        "additional_preferences": additional_preferences,
                    },
                )
            _, provider = scenario(name, now)
            if "city" in patch:
                from city_names import canonical_city

                name = canonical_city(prospective_req.city)[2]
                _, provider = scenario(name, now)
            result = Engine(provider, result_limit=3, max_age=timedelta(hours=24)).search(
                prospective_req, now
            )
            decisions = result.get("decisions", [])
            order = {"CONSTRAINTS_MET_NOT_LIVE": 0, "REVIEW": 1, "REJECT": 2}
            decisions = sorted(
                decisions,
                key=lambda d: (
                    order[d.status],
                    Decimal(d.base_total) if d.base_total is not None else Decimal("Infinity"),
                    str(d.identity),
                ),
            )
            cards = []
            for d in decisions[:3]:
                extra = (
                    provider.metadata.get(d.identity[3], {})
                    if isinstance(provider, ArchiveRU)
                    else {}
                )
                reasons = list(d.reasons) + extra.get("extra_reasons", [])
                status = (
                    "REVIEW" if reasons and d.status == "CONSTRAINTS_MET_NOT_LIVE" else d.status
                )
                observed = (
                    read(provider.rates)["finished_at_utc"]
                    if isinstance(provider, ArchiveTourMind)
                    else provider.capture["date"]
                    if isinstance(provider, ArchiveRU)
                    else "Часы синтетического примера"
                )
                cards.append(
                    {
                        "name": extra.get(
                            "name",
                            "Тестовый отель"
                            if name == "synthetic"
                            else "Тариф отеля " + d.identity[1],
                        ),
                        "room_name": extra.get("room_name", "Номер " + d.identity[2]),
                        "identity": jsonable(d.identity),
                        "status": status,
                        "reasons": reasons,
                        "reason_texts": [REASONS.get(r, r) for r in reasons],
                        "base_total": d.base_total,
                        "currency": d.currency,
                        "payable_for_method": d.payable_for_method,
                        "fee_method": d.fee_method,
                        "evidence": d.evidence,
                        "source_ref": d.source_ref,
                        "inventory": d.inventory,
                        "metadata": extra,
                        "quote_checkin": d.identity[4],
                        "quote_checkout": d.identity[5],
                        "price_basis": "За всех гостей и всё проживание",
                        "breakfast": False if name == "ru" else None,
                        "capacity": None,
                        "taxes_known": None,
                        "observed_at_or_date": observed,
                        "observation_precision": "DATE_ONLY"
                        if isinstance(provider, ArchiveRU)
                        else "CAPTURE_TIMESTAMP"
                        if isinstance(provider, ArchiveTourMind)
                        else "SYNTHETIC",
                    }
                )
            for card in cards:
                card.update(
                    card_facts(
                        provider,
                        next(
                            x.identity
                            for x in decisions
                            if jsonable(x.identity) == card["identity"]
                        ),
                    )
                )
            unchanged = [
                k
                for k in (
                    "city",
                    "country",
                    "checkin",
                    "checkout",
                    "rooms",
                    "currency",
                    "breakfast",
                )
                if k not in patch
            ]
            labels = {
                "budget": "бюджет",
                "breakfast": "требование завтрака",
                "checkin": "заезд",
                "checkout": "выезд",
                "rooms": "гости",
                "city": "город",
                "country": "страна",
            }
            summary = (
                f"Бюджет: {s.state.request.budget} → {patch['budget']} {s.state.request.currency}"
                if action == "budget"
                else f"Завтрак: {'обязателен' if patch['breakfast'] else 'не обязателен'}"
                if action == "constraints"
                else "Изменятся: " + ", ".join(labels.get(k, k) for k in patch)
            )
            status_impact = f"{len([c for c in cards if c['status'] != 'REJECT'])} из {len(cards)} тарифов не отклонены; неизвестные условия требуют проверки"
            changes = {
                k: {
                    "before": jsonable(getattr(s.state.request, k)),
                    "after": jsonable(getattr(prospective_req, k)),
                }
                for k in patch
                if getattr(s.state.request, k) != getattr(prospective_req, k)
            }
            from offline_destinations import stale_archive_prices

            stale = stale_archive_prices(
                prospective_req, s.history[0].request if s.history else s.state.request, cards
            )
            proposal = self._create_proposal(
                sid,
                s.state.revision,
                action,
                {
                    "patch": patch,
                    "changes": changes,
                    "scenario": name,
                    "prospective_request": jsonable(prospective_req),
                    "unchanged_fields": unchanged,
                    "status_impact": status_impact,
                    "prospective_cards": cards,
                    "summary": summary,
                    "additional_preferences": additional_preferences,
                    **stale,
                },
            )
            return proposal

    def preview_create(
        self,
        name,
        request,
        sid=None,
        revision=None,
        preferences=None,
        draft_id=None,
        draft_seq=None,
        source_result=None,
    ):
        with self.lock, self.store.transaction():
            request.validate()
            if sid:
                _, session = self._load(sid)
                if type(revision) is not int or revision != session.state.revision:
                    raise Conflict("STALE_REVISION")
            else:
                revision = None
            if draft_id:
                self._path(draft_id)
                if draft_seq is None:
                    draft_seq = self.start_draft(draft_id)
                newest = self.store.db.execute(
                    "SELECT seq FROM draft_latest_seq WHERE id=?", (draft_id,)
                ).fetchone()
                if newest and draft_seq < newest[0]:
                    raise Conflict("STALE_DRAFT_REQUEST")
                previous = self.store.db.execute(
                    "SELECT preview_id, seq FROM draft_groups WHERE id=?", (draft_id,)
                ).fetchone()
                if previous:
                    prev_seq = previous[1] or 0
                    if prev_seq > draft_seq:
                        raise Conflict("STALE_DRAFT_REQUEST")
                    self.store.db.execute(
                        "UPDATE proposals SET lifecycle='SUPERSEDED' WHERE id=? AND lifecycle='PENDING'",
                        (previous[0],),
                    )
            from offer_state import project

            cards, source = (
                project(jsonable(request), source_result)
                if source_result is not None
                else ([], None)
            )
            from scoped_budget import draft_scopes

            pending_scopes = draft_scopes(self, draft_id) if sid is None else None
            if pending_scopes and (pending_scopes.get("whole_trip") or pending_scopes.get("conflict")):
                whole = pending_scopes.get("whole_trip") or {}
                hotel = pending_scopes.get("hotel_only") or {}
                if (pending_scopes.get("conflict") or not whole.get("currency")
                        or whole.get("currency") != request.currency
                        or hotel.get("currency") != request.currency
                        or hotel.get("amount") != request.budget
                        or Decimal(hotel["amount"]) > Decimal(whole["amount"])):
                    raise InputError("DRAFT_BUDGET_SCOPE_REQUIRES_CLARIFICATION")
            proposal = self._create_proposal(
                sid,
                revision,
                "create",
                {
                    "scenario": name,
                    "draft_id": draft_id,
                    "pending_budget_scopes": pending_scopes,
                    "patch": jsonable(request),
                    "preferences": preferences or [],
                    "prospective_request": jsonable(request),
                    "prospective_cards": cards,
                    "source_mode": "live"
                    if source_result is not None or name == "live_dynamic"
                    else "archive",
                    "source_result": source,
                    "unchanged_fields": [],
                    "summary": "Новая поездка: " + request.city,
                    "status_impact": "Сохранится наблюдение поставщика; наличие и окончательная цена не подтверждены"
                    if source is not None
                    else "Тарифы будут проверены после подтверждения условий",
                },
            )
            if draft_id:
                self.store.db.execute(
                    "INSERT OR REPLACE INTO draft_groups(id, preview_id, seq) VALUES (?,?,?)",
                    (draft_id, proposal["preview_id"], draft_seq),
                )
            return proposal

    def preview_scoped_budget(self, sid, revision, spec, feasibility):
        """Preview a whole-trip limit. The hotel request is copied unchanged."""
        with self.lock, self.store.transaction():
            name, session = self._load(sid)
            if type(revision) is not int or revision != session.state.revision:
                raise Conflict("STALE_REVISION")
            self._check_scoped_budget(session, sid, {"patch": spec, "prospective_request": jsonable(session.state.request)})
            request = jsonable(session.state.request)
            cards = self.execute("read", sid=sid)["cards"]
            saved = (self.store.get_session(sid).get("budget_scopes") or {}).get("whole_trip") or {}
            return self._create_proposal(
                sid,
                session.state.revision,
                "scoped_budget",
                {
                    "patch": spec,
                    "changes": {
                        "whole_trip": {
                            "before": saved.get("amount"),
                            "after": spec["amount"],
                        }
                    },
                    "scenario": name,
                    "prospective_request": request,
                    "prospective_cards": cards,
                    "unchanged_fields": [
                        "city",
                        "country",
                        "checkin",
                        "checkout",
                        "rooms",
                        "budget",
                        "currency",
                        "breakfast",
                    ],
                    "summary": (
                        f"Лимит whole_trip → {spec['amount']} {spec['currency']}. "
                        f"Бюджет hotel_only {request['budget']} {request['currency']} без изменений."
                    ),
                    "status_impact": (
                        "Цены транспорта, трансфера и итога UNKNOWN. Наличие UNKNOWN. "
                        "Бюджет отеля не заменяется."
                    ),
                    "budget_scope": "whole_trip",
                    "feasibility": feasibility,
                },
            )

    def confirm_separate_whole_trip(self, sid, revision, amount, currency, confirmation):
        """Open a whole-trip Preview only after an explicit separate confirmation.

        The hotel request budget is not a base and is not written here.
        """
        from scoped_budget import (
            SEPARATE_WHOLE_TRIP_CONFIRMATION,
            _money,
            assessment,
        )

        if confirmation != SEPARATE_WHOLE_TRIP_CONFIRMATION:
            raise InputError("WHOLE_TRIP_CONFIRMATION_REQUIRED")
        if not isinstance(amount, str) or not isinstance(currency, str):
            raise InputError("BUDGET_SCOPE_INVALID")
        if len(currency) != 3 or any(char < "A" or char > "Z" for char in currency):
            raise InputError("BUDGET_SCOPE_INVALID")
        money = _money(amount)
        if money is None or money != amount:
            raise InputError("BUDGET_SCOPE_INVALID")
        spec = {
            "budget_scope": "whole_trip",
            "amount": money,
            "currency": currency,
            "op": "absolute",
            "scope_base": None,
            "percent": None,
        }
        feasibility = assessment(
            {
                "amount": money,
                "currency": currency,
                "scope": "whole_trip",
                "evidence": "USER_STATED",
            }
        )
        for component in feasibility["components"].values():
            if component["amount"] is not None:
                raise InputError("COMPONENT_PRICE_INVENTED")
        preview = self.preview_scoped_budget(sid, revision, spec, feasibility)
        output = self.execute("read", sid=sid)
        request = output["request"]
        if request["budget"] != preview["prospective_request"]["budget"]:
            raise InputError("HOTEL_BUDGET_OVERWRITE_FORBIDDEN")
        output.update(
            preview=preview,
            preview_id=preview["preview_id"],
            has_preview=True,
            whole_trip_confirmation=None,
            budget_assessment=feasibility,
            language_message=(
                f"Preview отдельного лимита whole_trip: {money} {currency}. "
                f"Бюджет hotel_only {request['budget']} {request['currency']} не изменён. "
                "Цены отеля, транспорта и трансфера UNKNOWN и не суммируются. "
                "Подтвердите Preview или отмените его."
            ),
            language={
                "source": "EXPLICIT_WHOLE_TRIP_CONFIRMATION",
                "model_calls": 0,
                "validated_intent": {"intent": "clarify", "values": {}, "needs": []},
                "state_changed": False,
            },
        )
        return output

    def _check_scoped_budget(self, session, sid, proposal):
        from scoped_budget import recompute

        patch = proposal["patch"]
        if set(patch) != {
            "budget_scope",
            "amount",
            "currency",
            "op",
            "scope_base",
            "percent",
        }:
            raise InputError("PATCH_FIELDS_NOT_ALLOWED")
        if patch["budget_scope"] != "whole_trip" or patch["op"] not in ("absolute", "relative"):
            raise InputError("BUDGET_SCOPE_INVALID")
        if patch["currency"] != session.state.request.currency:
            raise InputError("BUDGET_CURRENCY_CHANGE_REQUIRES_NEW_REQUEST")
        if recompute(patch) != patch["amount"]:
            raise InputError("BUDGET_SCOPE_MISMATCH")
        current = jsonable(session.state.request)
        for key in (
            "city",
            "country",
            "checkin",
            "checkout",
            "rooms",
            "budget",
            "currency",
            "breakfast",
        ):
            if proposal["prospective_request"].get(key) != current.get(key):
                raise InputError("HOTEL_BUDGET_OVERWRITE_FORBIDDEN")
        saved = (self.store.get_session(sid).get("budget_scopes") or {}).get("whole_trip")
        saved_amount = saved.get("amount") if isinstance(saved, dict) else None
        if patch["op"] == "relative" and saved_amount != patch["scope_base"]:
            raise Conflict("STALE_REVISION")

    def _create_proposal(self, sid, revision, kind, body):
        # Apply the prospective preference gates BEFORE signing the existing
        # one-use proposal. Persisted preferences only change during Commit.
        preferences = (self.store.get_session(sid) or {}).get("preferences", [])
        if kind == "create":
            preferences = body.get("preferences", [])
        elif kind == "preference":
            preferences = [*preferences, body["patch"]]
        else:
            preferences = [*preferences, *body.get("additional_preferences", [])]
        return self.store.create(sid, revision, kind, pet_project(body, preferences, pending=True))

    def preview_preference(self, sid, patch, revision):
        with self.lock, self.store.transaction():
            _, session = self._load(sid)
            if type(revision) is not int or revision != session.state.revision:
                raise Conflict("STALE_REVISION")
            if (
                set(patch) != {"text", "evidence_status"}
                or patch["evidence_status"] != "UNVERIFIED"
            ):
                raise InputError("PREFERENCE_INVALID")
            return self._create_proposal(
                sid,
                revision,
                "preference",
                {
                    "patch": patch,
                    "prospective_request": jsonable(session.state.request),
                    "prospective_cards": self.execute("read", sid=sid)["cards"],
                    "unchanged_fields": [
                        "city",
                        "country",
                        "checkin",
                        "checkout",
                        "rooms",
                        "budget",
                        "currency",
                        "breakfast",
                    ],
                    "summary": "Добавить пожелание: " + patch["text"],
                    "status_impact": "Пожелание не является подтверждением свойств гостиниц",
                },
            )

    def commit_proposal(self, sid=None, preview_id=None, revision=None):
        with self.lock, self.store.transaction():
            proposal = self.store.get(preview_id, sid, revision)
            if sid:
                name, s = self._load(sid)
                if s.state.revision != revision:
                    raise Conflict("STALE_REVISION")
            kind = proposal["action"]
            if kind == "create":
                name = proposal["scenario"]
                s = Session(State(request_from(proposal["prospective_request"])))
                result_sid = uuid.uuid4().hex
            else:
                if kind == "preference":
                    if (
                        set(proposal["patch"]) != {"text", "evidence_status"}
                        or proposal["patch"]["evidence_status"] != "UNVERIFIED"
                    ):
                        raise InputError("PREFERENCE_INVALID")
                    s.history.append(s.state)
                    s.state = State(s.state.request, s.state.revision + 1)
                elif kind == "scoped_budget":
                    self._check_scoped_budget(s, sid, proposal)
                    s.history.append(s.state)
                    s.state = State(s.state.request, s.state.revision + 1)
                    name = proposal.get("scenario", name)
                else:
                    if kind not in ("budget", "constraints", "refine"):
                        raise InputError("PREVIEW_OPERATION_INVALID")
                    allowed = (
                        {"budget"}
                        if kind == "budget"
                        else {"breakfast"}
                        if kind == "constraints"
                        else {
                            "budget",
                            "breakfast",
                            "checkin",
                            "checkout",
                            "rooms",
                            "city",
                            "country",
                        }
                    )
                    if not proposal["patch"] or set(proposal["patch"]) - allowed:
                        raise InputError("PATCH_FIELDS_NOT_ALLOWED")
                    from refinement import normalize

                    request = normalize(s.state.request, proposal["patch"])
                    checked_patch = {k: getattr(request, k) for k in proposal["patch"]}
                    s.apply(s.preview(checked_patch, revision))
                    name = proposal.get("scenario", name)
                result_sid = sid
            self.store.finish(preview_id, "COMMITTED", result_sid)
            self._save(
                result_sid,
                name,
                s,
                source_result=proposal["source_result"],
                source_mode=proposal["source_mode"],
            ) if "source_mode" in proposal else self._save(result_sid, name, s)
            additions = (
                proposal.get("additional_preferences", [])
                if kind in ("budget", "constraints", "refine")
                else []
            )
            if kind in ("create", "preference") or additions:
                body = self.store.get_session(result_sid)
                if kind == "create":
                    body["preferences"] = [
                        {"text": x, "evidence_status": "UNVERIFIED"}
                        for x in proposal.get("preferences", [])
                    ]
                    if proposal.get("pending_budget_scopes"):
                        body["pending_budget_scopes"] = proposal["pending_budget_scopes"]
                elif kind == "preference":
                    body["preferences"].append(proposal["patch"])
                else:
                    body["preferences"].extend(
                        {"text": x, "evidence_status": "UNVERIFIED"} for x in additions
                    )
                self.store.put_session(result_sid, body)
            if kind == "scoped_budget":
                body = self.store.get_session(result_sid)
                body["budget_scopes"] = {
                    "whole_trip": {
                        "amount": proposal["patch"]["amount"],
                        "currency": proposal["patch"]["currency"],
                        "evidence": "USER_STATED",
                    }
                }
                self.store.put_session(result_sid, body)
            self.store.invalidate(result_sid)
            out = self.execute("read", sid=result_sid)
            confirmed = (
                "Подтверждён только лимит whole_trip. Бюджет hotel_only не изменён. "
                "Цены частей маршрута и наличие остаются UNKNOWN. Это не тариф поставщика."
                if kind == "scoped_budget"
                else "Условия подтверждены. Поездка сохранена."
            )
            out.update(has_preview=False, language_message=confirmed)
            pending_whole = (proposal.get("pending_budget_scopes") or {}).get("whole_trip")
            if kind == "create" and pending_whole:
                from scoped_budget import assess

                turn = assess(
                    "Общий бюджет " + pending_whole["amount"] + " " + pending_whole["currency"],
                    s.state.request, None,
                )
                out["pending_budget_scopes"] = proposal["pending_budget_scopes"]
                out["budget_assessment"] = turn["assessment"]
                out["whole_trip_confirmation"] = turn["confirmation"]
                out["language_message"] += " " + turn["message"]
            return out

    def cancel_preview(self, sid=None, preview_id=None):
        with self.lock, self.store.transaction():
            if preview_id is None and sid:
                preview_id = self.store.current(sid).get("preview_id")
            if preview_id:
                row = self.store.db.execute(
                    "SELECT revision FROM proposals WHERE id=?", (preview_id,)
                ).fetchone()
                self.store.get(preview_id, sid, row[0] if row else None)
                self.store.finish(preview_id, "CANCELLED", sid)
            out = (
                self.execute("read", sid=sid)
                if sid
                else {"session_id": None, "request": None, "revision": None, "cards": []}
            )
            out.update(
                has_preview=False,
                language_message="Предложение отозвано. Условия поездки сохранены.",
            )
            return out

    def start_request(self, name, request):
        with self.lock:
            request.validate()
            scenario(name, datetime.now(timezone.utc))
            sid = uuid.uuid4().hex
            self._save(sid, name, Session(State(request)))
            return self.execute("read", sid=sid)

    def patch_conditions(self, sid, patch, revision):
        with self.lock:
            if set(patch) != {"breakfast"}:
                raise InputError("CONDITION_FIELDS_NOT_ALLOWED")
            name, s = self._load(sid)
            s.apply(s.preview(patch, revision))
            self._save(sid, name, s)
            return self.execute("read", sid=sid)

    def list_sessions(self, ids):
        if not isinstance(ids, list) or len(ids) > 30 or any(not isinstance(x, str) for x in ids):
            raise InputError("SESSION_LIST_INVALID")
        with self.lock:
            result = []
            for sid in dict.fromkeys(ids):
                self._path(sid)
                body = self.store.get_session(sid)
                if body:
                    r = body["state"]["request"]
                    result.append(
                        {
                            "session_id": sid,
                            "city": r["city"],
                            "checkin": r["checkin"],
                            "checkout": r["checkout"],
                            "budget": r["budget"],
                            "currency": r["currency"],
                            "revision": body["state"]["revision"],
                        }
                    )
            return {"sessions": result}

    def import_preview(self, payload, sid=None, revision=None, draft_id=None):
        if not isinstance(payload, dict) or set(payload) - {"request", "preferences"}:
            raise InputError("IMPORT_FIELDS_NOT_ALLOWED")
        request = request_from(payload["request"])
        from city_names import canonical_city

        city, country, name = canonical_city(request.city)
        if country != request.country:
            raise InputError("IMPORT_CITY_COUNTRY_MISMATCH")
        request = replace(request, city=city)
        preferences = payload.get("preferences", [])
        if not isinstance(preferences, list) or len(preferences) > 15:
            raise InputError("IMPORT_PREFERENCES_INVALID")
        if any(
            not isinstance(p, dict)
            or not isinstance(p.get("text"), str)
            or not 1 <= len(p["text"]) <= 1500
            for p in preferences
        ):
            raise InputError("IMPORT_PREFERENCES_INVALID")
        return self.preview_create(
            name, request, sid, revision, [p["text"] for p in preferences], draft_id
        )

    def _path(self, sid):
        if (
            not isinstance(sid, str)
            or len(sid) != 32
            or any(c not in "0123456789abcdef" for c in sid)
        ):
            raise InputError("INVALID_SESSION_ID")
        return self.storage / (sid + ".json")

    def _store_source(self, result):
        if result is None:
            return None
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        identifier = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self.store.db.execute(
            "INSERT OR IGNORE INTO offer_observations VALUES (?,?)", (identifier, encoded)
        )
        return identifier

    def _source_result(self, body):
        identifier = body.get("source_ref")
        if identifier:
            row = self.store.db.execute(
                "SELECT payload FROM offer_observations WHERE id=?", (identifier,)
            ).fetchone()
            if not row or hashlib.sha256(row[0].encode("utf-8")).hexdigest() != identifier:
                raise InputError("SOURCE_OBSERVATION_MISSING_OR_CORRUPT")
            return json.loads(row[0])
        return body.get("source_result")

    def _save(self, sid, name, session, **source_update):
        self._path(sid)
        body = {
            "schema": 1,
            "scenario": name,
            "state": jsonable(session.state),
            "history": jsonable(session.history),
        }
        old = self.store.get_session(sid) or {}
        preferences = old.get("preferences", [])
        history = old.get("preference_history", [])
        scenario_history = old.get(
            "scenario_history", [old.get("scenario", name)] * len(old.get("history", []))
        )
        scopes = json.loads(json.dumps(old.get("budget_scopes") or {"whole_trip": None}))
        scope_history = old.get("budget_scope_history", [])
        if not isinstance(scope_history, list):
            scope_history = []
        if len(session.history) > len(old.get("history", [])):
            history.append(preferences)
            scenario_history.append(old.get("scenario", name))
            scope_history.append(scopes)
        elif len(session.history) < len(old.get("history", [])):
            if history:
                preferences = history.pop()
            if scenario_history:
                body["scenario"] = scenario_history.pop()
            if scope_history:
                scopes = scope_history.pop()
        body.update(preferences=preferences, preference_history=history)
        body["budget_scopes"] = scopes
        body["budget_scope_history"] = scope_history
        if old.get("state", {}).get("request") == body["state"]["request"]:
            if old.get("pending_budget_scopes"):
                body["pending_budget_scopes"] = old["pending_budget_scopes"]
        body["scenario_history"] = scenario_history
        sources = old.get(
            "offer_source_history",
            [{"mode": "archive", "result": None}] * len(old.get("history", [])),
        )
        mode = old.get("source_mode", "live" if name == "live_dynamic" else "archive")
        result = self._source_result(old)
        if len(session.history) > len(old.get("history", [])):
            sources.append(
                {
                    "mode": mode,
                    "source_ref": old.get("source_ref"),
                    "source_result": old.get("source_result"),
                }
            )
        elif len(session.history) < len(old.get("history", [])):
            restored = sources.pop() if sources else {"mode": mode, "result": None}
            mode, result = restored["mode"], self._source_result(restored)
        if old.get("state", {}).get("request") != body["state"]["request"] and len(
            session.history
        ) >= len(old.get("history", [])):
            result = None
        mode = source_update.get("source_mode", mode)
        result = source_update.get("source_result", result)
        body.update(
            source_mode=mode, source_ref=self._store_source(result), offer_source_history=sources
        )
        self.store.put_session(sid, body)

    def _load(self, sid):
        path = self._path(sid)
        d = self.store.get_session(sid)
        if d is None:
            d = read(path)
            self.store.put_session(sid, d)
        if d.get("schema") != 1:
            raise InputError("UNKNOWN_SESSION_SCHEMA")
        s = Session(
            State(
                request_from(d["state"]["request"]),
                d["state"]["revision"],
                tuple(d["state"]["selected_offer"]) if d["state"].get("selected_offer") else None,
            )
        )
        s.history = [
            State(
                request_from(x["request"]),
                x["revision"],
                tuple(x["selected_offer"]) if x.get("selected_offer") else None,
            )
            for x in d["history"]
        ]
        return d["scenario"], s

    def execute(
        self,
        action,
        sid=None,
        name="synthetic",
        budget=None,
        revision=None,
        now=None,
        preview_id=None,
    ):
        now = now or datetime.now(timezone.utc)
        from scoped_budget import public_budget_scopes, pending_scope_output

        with self.lock, self.store.transaction():
            if action == "commit":
                return self.commit_proposal(sid=sid, preview_id=preview_id, revision=revision)
            elif action == "cancel_preview":
                return self.cancel_preview(sid=sid, preview_id=preview_id)
            elif action == "start":
                req, provider = scenario(name, now)
                s = Session(State(req))
                sid = uuid.uuid4().hex
                self._save(sid, name, s)
            else:
                name, s = self._load(sid)
                provider = (
                    None
                    if self.store.get_session(sid).get("source_mode") == "live"
                    or name == "live_dynamic"
                    else scenario(name, now)[1]
                )
                if action == "budget":
                    s.apply(s.preview({"budget": budget}, revision))
                    self._save(sid, name, s)
                    self.store.invalidate(sid)
                elif action == "undo":
                    s.undo(revision)
                    self._save(sid, name, s)
                    self.store.invalidate(sid)
                    self.store.event(sid, "UNDO", {"revision": s.state.revision})
                    name, s = self._load(sid)
                    provider = (
                        None
                        if self.store.get_session(sid).get("source_mode") == "live"
                        or name == "live_dynamic"
                        else scenario(name, now)[1]
                    )
                elif action != "read":
                    raise InputError("UNKNOWN_ACTION")
            body = self.store.get_session(sid)
            if body.get("source_mode") == "live" or name == "live_dynamic":
                from offer_state import project

                raw = self._source_result(body)
                cards, source = (
                    project(jsonable(s.state.request), raw, now) if raw is not None else ([], None)
                )
                if source is not None:
                    source.update(session_id=sid, revision=s.state.revision)
                    if body.get("source_snapshot_id"):
                        source["source_snapshot_id"] = body["source_snapshot_id"]
                return pet_project({
                    "session_id": sid,
                    "scenario": name,
                    "scenario_label": "Гостиничный источник",
                    "revision": s.state.revision,
                    "request": jsonable(s.state.request),
                    "can_undo": bool(s.history),
                    "result_status": source.get("status", "REVIEW")
                    if source is not None
                    else "SOURCE_NOT_REQUESTED",
                    "source_rows": len(source["offers"]) if source is not None else 0,
                    "issues": [],
                    "cards": cards,
                    "source_result": source,
                    "booking_performed": False,
                    "selected_offer": dict(
                        body["selected_supplier_offer"]["value"],
                        snapshot_stale=source["snapshot_stale"],
                    )
                    if source is not None
                    and (body.get("selected_supplier_offer") or {}).get("revision")
                    == s.state.revision
                    else None,
                    "provenance": {
                        "mode": next(
                            (
                                o.get("source")
                                for o in (source or {}).get("offers", [])
                                if o.get("source")
                            ),
                            "TOURMIND_LIVE_RATE",
                        )
                        if (source or {}).get("source_kind")
                        else "TOURMIND_LIVE_RATE",
                        "supplier_calls": 0,
                        "tool_operations": 1,
                        "paid_calls": 0,
                        "observation_only": True,
                        "model": "NONE_FOR_THIS_CORE_RECHECK",
                    },
                    "local_processing_ms": 0,
                    "summary": "Сохранённая выдача поставщика. Наличие и окончательный счёт не подтверждены."
                    if source is not None
                    else "Для этих условий нужен новый поиск. Архивные цены не подставлены.",
                    "history": [
                        {"revision": x.revision, "request": jsonable(x.request)} for x in s.history
                    ],
                    "preferences": body.get("preferences", []),
                    "budget_scopes": public_budget_scopes(
                        jsonable(s.state.request), body.get("budget_scopes")
                    ),
                    **pending_scope_output(s.state.request, body),
                    "pending_preview": self.store.current(sid),
                    "mode": "SAVED_SUPPLIER_OBSERVATION",
                }, body.get("preferences", []))
            started = time.perf_counter()
            result = s.recheck(Engine(provider, result_limit=3, max_age=timedelta(hours=24)), now)[
                "result"
            ]
            elapsed = (time.perf_counter() - started) * 1000
            decisions = result.get("decisions", [])
            order = {"CONSTRAINTS_MET_NOT_LIVE": 0, "REVIEW": 1, "REJECT": 2}
            decisions = sorted(
                decisions,
                key=lambda d: (
                    order[d.status],
                    Decimal(d.base_total) if d.base_total is not None else Decimal("Infinity"),
                    str(d.identity),
                ),
            )
            cards = []
            for d in decisions[:3]:
                extra = (
                    provider.metadata.get(d.identity[3], {})
                    if isinstance(provider, ArchiveRU)
                    else {}
                )
                reasons = list(d.reasons) + extra.get("extra_reasons", [])
                status = (
                    "REVIEW" if reasons and d.status == "CONSTRAINTS_MET_NOT_LIVE" else d.status
                )
                observed = (
                    read(provider.rates)["finished_at_utc"]
                    if isinstance(provider, ArchiveTourMind)
                    else provider.capture["date"]
                    if isinstance(provider, ArchiveRU)
                    else "Часы синтетического примера"
                )
                cards.append(
                    {
                        "name": extra.get(
                            "name",
                            "Тестовый отель"
                            if name == "synthetic"
                            else "Тариф отеля " + d.identity[1],
                        ),
                        "room_name": extra.get("room_name", "Номер " + d.identity[2]),
                        "identity": jsonable(d.identity),
                        "status": status,
                        "reasons": reasons,
                        "reason_texts": [REASONS.get(r, r) for r in reasons],
                        "base_total": d.base_total,
                        "currency": d.currency,
                        "payable_for_method": d.payable_for_method,
                        "fee_method": d.fee_method,
                        "evidence": d.evidence,
                        "source_ref": d.source_ref,
                        "inventory": d.inventory,
                        "metadata": extra,
                        "quote_checkin": d.identity[4],
                        "quote_checkout": d.identity[5],
                        "price_basis": "За всех гостей и всё проживание",
                        "breakfast": False if name == "ru" else None,
                        "capacity": None,
                        "taxes_known": None,
                        "observed_at_or_date": observed,
                        "observation_precision": "DATE_ONLY"
                        if isinstance(provider, ArchiveRU)
                        else "CAPTURE_TIMESTAMP"
                        if isinstance(provider, ArchiveTourMind)
                        else "SYNTHETIC",
                    }
                )
            for card in cards:
                card.update(
                    card_facts(
                        provider,
                        next(
                            x.identity
                            for x in decisions
                            if jsonable(x.identity) == card["identity"]
                        ),
                    )
                )
            provenance = {
                "mode": "SYNTHETIC"
                if name in ("synthetic", "error", "empty")
                else "ARCHIVED_RESPONSE",
                "replayed_at": now.isoformat(),
                "supplier_calls": 0,
                "tool_operations": 1,
                "paid_calls": 0,
                "paid_calls_scope": "Supplier operations only",
                "model": "NONE_FOR_THIS_CORE_RECHECK",
                "natural_language_understanding_verified": False,
            }
            if isinstance(provider, ArchiveTourMind):
                provenance.update(
                    captured_at=read(provider.rates)["finished_at_utc"],
                    source_sha256=sha(provider.rates),
                    detail_sha256=sha(provider.details),
                )
            if isinstance(provider, ArchiveRU):
                provenance.update(
                    captured_date=provider.capture["date"],
                    observation_precision="DATE_ONLY_LOWER_BOUND",
                    source_sha256=sha(provider.search_path),
                )
            output = {
                "session_id": sid,
                "scenario": name,
                "scenario_label": SCENARIOS[name],
                "revision": s.state.revision,
                "request": jsonable(s.state.request),
                "can_undo": bool(s.history),
                "result_status": result["status"],
                "source_rows": result.get("source_rows", 0),
                "issues": result.get("issues", []),
                "cards": cards,
                "provenance": provenance,
                "local_processing_ms": elapsed,
                "summary": "Ошибка источника; результат неизвестен."
                if result["status"] == "SOURCE_ERROR"
                else "Источник вернул пустую выдачу."
                if not decisions
                else f"Показано {len(cards)} из {len(decisions)} тарифов. Наличие не проверено.",
            }
            receipt = self.storage / "language" / (sid + ".json")
            if receipt.exists():
                output["last_language_receipt"] = read(receipt)
            output["history"] = [
                {"revision": x.revision, "request": jsonable(x.request)} for x in s.history
            ]
            output["preferences"] = self.store.get_session(sid).get("preferences", [])
            output["budget_scopes"] = public_budget_scopes(
                jsonable(s.state.request), self.store.get_session(sid).get("budget_scopes")
            )
            output.update(pending_scope_output(s.state.request, body))
            output["pending_preview"] = self.store.current(sid)
            output["mode"] = "OFFLINE_DEMO"
            from offline_destinations import stale_archive_prices

            output.update(
                stale_archive_prices(
                    s.state.request, s.history[0].request if s.history else s.state.request, cards
                )
            )
            if output.get("price_status") == "STALE":
                output["summary"] = "STALE: даты изменены; архивные цены требуют нового поиска."
            return pet_project(output, body.get("preferences", []))


class Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, *args):
        pass

    def reply(self, code, data, kind="application/json; charset=utf-8"):
        body = (
            data
            if isinstance(data, bytes)
            else json.dumps(data, ensure_ascii=False).encode("utf-8")
        )
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def local(self):
        port = self.server.server_address[1]
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise InputError("HOST_NOT_ALLOWED")
        if self.headers.get("Origin") not in (None, "http://" + host):
            raise InputError("ORIGIN_NOT_ALLOWED")

    def do_GET(self):
        try:
            self.local()
            url = urlparse(self.path)
            if url.path == "/api/config":
                return self.reply(200, {"mode": "OFFLINE_FULL_UI", "nonce": None})
            if url.path == "/api/model/status":
                return self.reply(200, self.app.model_client.status())
            if url.path == "/api/supplier/status":
                from supplier_etg_v3 import public_supplier_status

                return self.reply(200, public_supplier_status())
            if url.path == "/api/live/latest":
                if not self.app.live_enabled:
                    raise InputError("LIVE_TEST_DISABLED")
                with self.app.lock:
                    row = self.app.store.db.execute(
                        "SELECT result_sid FROM proposals WHERE kind='create' AND lifecycle='COMMITTED' AND result_sid IS NOT NULL ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()
                    if not row:
                        return self.reply(404, {"error": "NO_LAB_TRIP_YET"})
                    return self.reply(200, self.app.execute("read", sid=row[0]))
            if url.path == "/api/health":
                return self.reply(
                    200,
                    {
                        "app": "TRAVELTECH_REFERENCE",
                        "root_id": hashlib.sha256(str(ROOT).encode()).hexdigest(),
                        "mode": "LIVE_SYNTHETIC_TEST" if self.app.live_enabled else "OFFLINE_DEMO",
                    },
                )
            if url.path == "/api/proposal/status":
                query = parse_qs(url.query)
                with self.app.lock:
                    return self.reply(
                        200,
                        self.app.store.status(
                            query.get("id", [None])[0], query.get("sid", [None])[0]
                        ),
                    )
            if url.path == "/api/proposal":
                query = parse_qs(url.query)
                identifier = query.get("id", [None])[0]
                owner = query.get("sid", [None])[0]
                with self.app.lock:
                    row = self.app.store.db.execute(
                        "SELECT revision FROM proposals WHERE id=?", (identifier,)
                    ).fetchone()
                    return self.reply(
                        200, self.app.store.get(identifier, owner, row[0] if row else None)
                    )
            if url.path == "/api/session":
                return self.reply(
                    200, self.app.execute("read", sid=parse_qs(url.query).get("id", [None])[0])
                )
            if url.path == "/api/preview":
                sid = parse_qs(url.query).get("id", [None])[0]
                with self.app.lock:
                    return self.reply(200, self.app.store.current(sid))
            files = {
                "/": "index.html",
                "/ui.js": "ui.js",
                "/style.css": "style.css",
                "/full.js": "full.js",
                "/full.css": "full.css",
            }
            if url.path not in files:
                return self.reply(404, {"error": "NOT_FOUND"})
            kinds = {
                "/": "text/html; charset=utf-8",
                "/ui.js": "text/javascript; charset=utf-8",
                "/style.css": "text/css; charset=utf-8",
                "/full.js": "text/javascript; charset=utf-8",
                "/full.css": "text/css; charset=utf-8",
            }
            self.reply(200, (Path(__file__).parent / files[url.path]).read_bytes(), kinds[url.path])
        except (ValueError, KeyError, TypeError, FileNotFoundError) as e:
            self.reply(400, {"error": str(e)})

    def do_POST(self):
        try:
            self.local()
            if self.path not in (
                "/api/action",
                "/api/language",
                "/api/agent",
                "/api/model/discover",
                "/api/commit",
                "/api/budget-scope",
                "/api/preview",
                "/api/shadow",
                "/api/sessions",
                "/api/import",
                "/api/live/sample",
                "/api/live/replay",
                "/api/live/recovery",
                "/api/free/sample",
            ):
                return self.reply(404, {"error": "NOT_FOUND"})
            if self.headers.get("Content-Type") != "application/json":
                raise InputError("JSON_CONTENT_TYPE_REQUIRED")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= (65536 if self.path == "/api/import" else 16384):
                raise InputError("BODY_SIZE_LIMIT")
            data = safe_json(self.rfile.read(size))
            if not isinstance(data, dict):
                raise InputError("JSON_OBJECT_REQUIRED")
            if self.path == "/api/agent":
                if set(data) - {"text", "sid", "revision", "draft_id"}:
                    raise InputError("AGENT_FIELDS_NOT_ALLOWED")
                from agent_cycle import AgentCycle

                return self.reply(200, AgentCycle(self.app).run(**data))
            if self.path == "/api/sessions":
                if set(data) != {"ids"}:
                    raise InputError("SESSION_LIST_INVALID")
                return self.reply(200, self.app.list_sessions(data["ids"]))
            if self.path == "/api/import":
                if set(data) - {"payload", "sid", "revision", "draft_id"}:
                    raise InputError("IMPORT_FIELDS_NOT_ALLOWED")
                proposal = self.app.import_preview(**data)
                return self.reply(
                    200,
                    {
                        "preview": proposal,
                        "preview_id": proposal["preview_id"],
                        "has_preview": True,
                        "session_id": data.get("sid"),
                        "language_message": "Проверьте импортируемые условия. Цены и факты из файла не используются.",
                    },
                )
            if self.path == "/api/shadow":
                if not isinstance(data, dict) or set(data) - {"scenario", "sid"}:
                    raise InputError("SHADOW_FIELDS_NOT_ALLOWED")
                before = self.app.execute("read", sid=data["sid"]) if data.get("sid") else None
                result = self.app.router.simulate(data["scenario"])
                with self.app.lock:
                    self.app.store.event(data.get("sid"), "SHADOW_ROUTE", result)
                after = self.app.execute("read", sid=data["sid"]) if data.get("sid") else None
                result["state_preserved"] = (
                    before == after
                    if before is None
                    else before["request"] == after["request"]
                    and before["revision"] == after["revision"]
                )
                return self.reply(200, result)
            if self.path == "/api/free/sample":
                if not hasattr(self.app, "free_language"):
                    raise InputError("FREE_RESOURCE_LAB_DISABLED")
                if set(data) - {"case", "draft_id"} or data.get("case") != "free_search":
                    raise InputError("ONLY_FIXED_SYNTHETIC_FREE_CASE")
                from free_resources_client import SYNTHETIC_TEXT

                return self.reply(
                    200,
                    self.app.free_language.execute(
                        SYNTHETIC_TEXT,
                        "real",
                        sid=None,
                        revision=None,
                        preview=True,
                        draft_id=data.get("draft_id"),
                    ),
                )
            if self.path == "/api/live/recovery":
                if not self.app.live_enabled or not hasattr(self.app, "recovery_language"):
                    raise InputError("OFFLINE_RECOVERY_UNAVAILABLE")
                if (
                    set(data) - {"case", "draft_id"}
                    or data.get("case") != "simulated_llm_unavailable"
                ):
                    raise InputError("RECOVERY_EXACT_CASE_REQUIRED")
                return self.reply(
                    200,
                    self.app.recovery_language.execute(
                        self.app.recovery_language.client.text,
                        "real",
                        sid=None,
                        revision=None,
                        preview=True,
                        draft_id=data.get("draft_id"),
                    ),
                )
            if self.path == "/api/live/replay":
                if not self.app.live_enabled or not hasattr(self.app, "replay_language"):
                    raise InputError("OFFLINE_REPLAY_UNAVAILABLE")
                if set(data) - {"case", "draft_id"} or data.get("case") != "recorded_mimo_search":
                    raise InputError("REPLAY_EXACT_CASE_REQUIRED")
                from mimo_replay_client import RecordedMimoReplayClient

                return self.reply(
                    200,
                    self.app.replay_language.execute(
                        self.app.replay_language.client.text,
                        "real",
                        sid=None,
                        revision=None,
                        preview=True,
                        draft_id=data.get("draft_id"),
                    ),
                )
            if self.path == "/api/live/sample":
                if (
                    not self.app.live_enabled
                    or self.app.model_client.status().get("mode") == "REPLAY_ONLY"
                ):
                    raise InputError("LIVE_TEST_DISABLED")
                if (
                    set(data) - {"case", "draft_id"}
                    or data.get("case") != "injected_primary_outage"
                ):
                    raise InputError("LIVE_SYNTHETIC_CASE_REQUIRED")
                from failover_probe import PROMPT

                text = PROMPT
                return self.reply(
                    200,
                    self.app.language.execute(
                        text,
                        "real",
                        sid=None,
                        revision=None,
                        preview=True,
                        draft_id=data.get("draft_id"),
                    ),
                )
            if self.path == "/api/language":
                if data.get("mode") == "real":
                    raise InputError("LIVE_USER_TEXT_NOT_AUTHORIZED_USE_SAMPLE")
                if not isinstance(data, dict) or set(data) - {
                    "text",
                    "mode",
                    "sid",
                    "revision",
                    "preview",
                    "draft_id",
                }:
                    raise InputError("LANGUAGE_FIELDS_NOT_ALLOWED")
                preview = True
                return self.reply(
                    200,
                    self.app.language.execute(
                        text=data["text"],
                        mode=data["mode"],
                        sid=data.get("sid"),
                        revision=data.get("revision"),
                        preview=preview,
                        draft_id=data.get("draft_id"),
                    ),
                )
            if self.path == "/api/commit":
                if not isinstance(data, dict) or set(data) - {"sid", "preview_id", "revision"}:
                    raise InputError("COMMIT_FIELDS_NOT_ALLOWED")
                return self.reply(200, self.app.commit_proposal(**data))
            if self.path == "/api/budget-scope":
                if set(data) != {
                    "action",
                    "sid",
                    "revision",
                    "amount",
                    "currency",
                    "confirmation",
                }:
                    raise InputError("BUDGET_SCOPE_FIELDS_NOT_ALLOWED")
                if data.get("action") != "confirm_separate_whole_trip":
                    raise InputError("WHOLE_TRIP_CONFIRMATION_REQUIRED")
                return self.reply(
                    200,
                    self.app.confirm_separate_whole_trip(
                        sid=data["sid"],
                        revision=data["revision"],
                        amount=data["amount"],
                        currency=data["currency"],
                        confirmation=data["confirmation"],
                    ),
                )
            if self.path == "/api/preview":
                if data.get("mode") == "real":
                    raise InputError("LIVE_USER_TEXT_NOT_AUTHORIZED_USE_SAMPLE")
                if not isinstance(data, dict) or set(data) - {"text", "mode", "sid", "revision"}:
                    raise InputError("PREVIEW_FIELDS_NOT_ALLOWED")
                return self.reply(
                    200,
                    self.app.language.execute(
                        text=data["text"],
                        mode=data["mode"],
                        sid=data.get("sid"),
                        revision=data.get("revision"),
                        preview=True,
                    ),
                )
            if self.path == "/api/model/discover":
                raise InputError("LIVE_TEST_DISABLED")
            if data.get("action") == "budget":
                raise InputError("PREVIEW_CONFIRM_REQUIRED")
            if not isinstance(data, dict) or set(data) - {
                "action",
                "sid",
                "name",
                "budget",
                "revision",
                "preview_id",
            }:
                raise InputError("ACTION_FIELDS_NOT_ALLOWED")
            self.reply(200, dispatch(self.app, data))
        except Conflict as e:
            self.reply(409, {"error": str(e)})
        except ModelError as e:
            self.reply(503, {"error": str(e), "automatic_retries": 0, "state_changed": False})
        except AdapterError as e:
            self.reply(503, {"error": str(e), "automatic_retries": 0, "state_changed": False})
        except (ValueError, KeyError, TypeError, FileNotFoundError) as e:
            self.reply(400, {"error": str(e)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--storage", type=Path, default=Path(__file__).parent / "runtime")
    parser.add_argument("--replay", choices=SCENARIOS)
    args = parser.parse_args()
    app = Application(args.storage)
    if args.replay:
        first = app.execute("start", name=args.replay)
        sid = first["session_id"]
        lowered = app.execute("budget", sid=sid, budget="100", revision=0)
        restored = app.execute("undo", sid=sid, revision=1)
        print(
            json.dumps(
                {"initial": first, "lowered": lowered, "restored": restored},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    Handler.app = app
    server = ExclusiveHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Offline TravelTech: http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
