# Карта кода TravelTech

Срез поставленной `main` на 26.10.2026, `bae5a6b`: Docker #104/#120 и TripPlan helper #116 влиты, UI #118 влит (только фиксированный MOCK). Ниже разделены пользовательский runtime, фиксированное демо и изолированные адаптеры. Это карта кода, не результат нового запуска.

## Основной путь простого запроса

```text
experimental/index.html + ui.js
  → /api/language
  → natural_language.py / refinement.py / city_names.py
  → geo_city_resolver.py, если требуется строгая проверка города
  → app.py: Preview (предложение)
  → full_server.py: /api/commit
  → app.py + SQLite: подтверждённая поездка
  → full.js: /api/data/search
  → supplier_router.py → supplier_liteapi.py
  → live_snapshot_store.py → full.js / ui.js / offer_sort.js
```

Без `--real` внешний поиск недоступен. Успешная локальная демонстрация не доказывает, что тарифы получены из LiteAPI.

## Подсистемы

| Область | Файлы | Назначение |
|---|---|---|
| Внешний вид | `experimental/index.html`, `style.css`, `full.css` | Разметка и оформление |
| Диалог | `experimental/ui.js` | Чат, предпросмотр и основной список |
| Детали гостиниц | `experimental/full.js`, `offer_sort.js` | Карточки, сравнение и общая сортировка |
| HTTP | `experimental/full_server.py`, `request_journal.py` | Обработчики, nonce, владелец, ревизии и квитанции |
| Домен | `experimental/app.py`, `prototype/base/` | Поездки, подтверждения и состояние |
| Снимки тарифов | `experimental/live_snapshot_store.py` | Привязка результатов к поездке и её версии |
| Понимание текста | `experimental/natural_language.py`, `refinement.py`, `city_names.py`, `geo_city_resolver.py` | Извлечение и проверка условий |
| Источники | `experimental/supplier_router.py`, `supplier_liteapi.py`, `live_data.py` | Read-only внешние данные |
| TripPlan helper #116 | `experimental/trip_plan.py`, `docs/contracts/trip_plan_v0.json` | Валидация/суммы и фиксированный MOCK; demo endpoint в `full_server.py`, без сессии и поиска |
| Карточка TripPlan | `experimental/trip_plan_card.js`, `trip_plan_card.css`, `trip_plan_ui.js`, `ui.js` | Встроена в основной интерфейс по #118; fixed MOCK, нет планирования по сессии |
| Pilot #112 | `experimental/supplier_pilot.py` | Изолированный read-only opt-in, не runtime гостиничного поиска |
| Трансферы #114 | `experimental/transfer_sources/` | Изолированный opt-in, не подключён к TripPlan demo или пользовательскому runtime |
| FX/POI #115 | `experimental/enrichment/` | Изолированный opt-in Frankfurter и POI, не замена существующего `live_data.py` |
| Docker #104/#120 | `Dockerfile`, `docker-compose.yml`, `.dockerignore` | Офлайн-образ, 8080, health, non-root; явная упаковка канонического MOCK fixture |
| Skills-агент | `experimental/skill_system/runtime.py`, `backend.py`, `contracts.py`, `answer_grounding.py` | Контекст, проверка предложения и доменные правила |
| Модельные маршруты | `skill_system/task_routing.py`, `logicc_ladder.py`, `safe_rotation.py`, `transport.py` | Выбор, учёт и ограничения моделей |
| Проверки | `tests/`, `scripts/check_local.py` | Регрессионные, HTTP и браузерные тесты |

Точное описание API и версий — [API_AND_STATE.md](API_AND_STATE.md). Работа с моделями и известное наличие OmniRush в коде — [AI_ROUTING.md](AI_ROUTING.md).

## TripPlan и изолированные источники

`GET /api/trip_plan/demo` вызывает `demo_trip_plan()` и читает [канонический MOCK](contracts/trip_plan_v0.json). `assemble_trip_plan()` пересчитывает сумму через Decimal, проверяет scope бюджета, валюты, даты и ревизию. Неизвестная цена остаётся `null`; разные валюты не складываются; при смене ревизии `stale_offer()` снимает зависимые от дат цены. Helper не получает параметры сессии, не делает поиск и не вызывает живой Ignav. Это **FIXED MOCK**, не персональный план.

Контракт: [TRIP_PLAN_V0.md](contracts/TRIP_PLAN_V0.md). Границы отдельных адаптеров: [Pilot](research/PILOT_ADAPTER_20261025.md), [трансферы](transfer/LIVE_AIRPORT_TRANSFERS_20261025.md), [FX/POI](enrichment/FX_AND_POI.md). Эти пакеты не подключены к `full_server.py`, `app.py` и `supplier_router.py`; их импорт и явный opt-in в отдельном процессе не означают интеграцию в основной сценарий.

Docker проверен как Linux/amd64-образ через WSL 2 в [отчёте #104](qa/ISSUE_30_DOCKER_REPORT.md). Свежий native Linux proof итоговой сборки ещё не подтверждён. `.dockerignore` ограничивает контекст образа; он не очищает приватный Git для [официальной сдачи](REPOSITORY_WORKFLOW.md).

## С чего начать разработчику

**Фронтенд:** пройти чат → Preview → commit → карточки, открыть `ui.js`, `full.js`, HTML/CSS. Не менять модель поездки через визуальные хаки.

**AI/NLP:** сначала `natural_language.py` и `refinement.py`, затем `skill_system/runtime.py`, `backend.py` и `contracts.py`. Различайте ошибки распознавания, JSON-контрактов и провайдера.

**Сервер/база:** изучить `full_server.py`, `app.py`, ревизии и тесты сессий. Новый сервис допускается только с согласованными входами, выходами, ошибками и интеграционными тестами.

**Гостиницы:** `supplier_liteapi.py`, `supplier_router.py` и тесты нормализации. Не подменять реальные ответы архивными тарифами.

Java/Spring Boot сейчас **не существует** как компонент приложения. TripPlan helper уже есть, но генерация полной поездки из запроса, интеграция её источников/пересборки и путь до корзины/MockOrder отсутствуют. Бронирований и платежей нет; готовность production не подтверждена.

## Осторожность при рефакторинге

`experimental/` содержит рабочий код и исследования, а `audit/`, `research/`, `vendor/` хранят исторические/внешние части. Перемещение каталогов может нарушить относительные пути. `ui.js`, `full.js`, `full_server.py` крупные: выносите по одной функции при наличии теста, не одновременно с изменением поведения.

## Проверки

```bash
python -m pip install -r requirements-dev.txt
python scripts/check_local.py
python scripts/check_local.py --suite full
```

Скрипт проверяет Python, JavaScript при наличии Node, тесты и локальный smoke. Для браузера нужны Edge и Playwright. Проверка реального обращения LiteAPI sandbox отдельная. Облачные GitHub Actions не требуются.

Перед PR в [CONTRIBUTING.md](../CONTRIBUTING.md) описаны правила. Актуальная схема — [ARCHITECTURE.md](ARCHITECTURE.md).
