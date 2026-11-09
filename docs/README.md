> **Работаем в [Ai-b0ss/traveltech](https://github.com/Ai-b0ss/traveltech).** В официальный itam-team переносим только main. Issues, PR и обсуждения остаются в TravelTech. [Схема работы](REPOSITORY_WORKFLOW.md).

# Документы TravelTech

Сверено 24 октября 2026 с официальной main после переноса. Здесь разделены **реально работающая архитектура**, **отчёты о прошлых изменениях** и **предложения**. Документы не подтверждают автоматически состояние запущенного сегодня сайта.

## Действующая документация

- [ARCHITECTURE.md](ARCHITECTURE.md) — реальная схема системы, MVP и Skills.
- [API_AND_STATE.md](API_AND_STATE.md) — запросы, версии, SQLite, доверенные границы.
- [AI_ROUTING.md](AI_ROUTING.md) — условный Auto, настройка моделей и поведение резерва.
- [CODE_GUIDE.md](CODE_GUIDE.md) — входные точки и области ответственности в исходниках.
- [TEAM_START.md](TEAM_START.md) — самостоятельный первый запуск.
- [CONFIGURATION.md](CONFIGURATION.md) — offline, реальные обращения, sandbox и ключи.
- [PROJECT_STATUS.md](PROJECT_STATUS.md) — что реализовано и что нет.
- [RELEASE_GATE.md](RELEASE_GATE.md) — датированные результаты проверок и границы демонстрации.
- [CONTRIBUTING.md](../CONTRIBUTING.md) — разработка в ветках, PR и локальные тесты.
- [SECURITY.md](../SECURITY.md) — безопасность источников, данных и секретов.

## Датированные материалы

- [Issue #63: Ignav smoke](research/ISSUE_63_IGNAV_SMOKE.md) — подготовленный
  изолированный клиент, реальный no-key 401; авторизованная выдача ещё не проверена.

`BRANCH_AUDIT.md`, `ISTANBUL_DIALOGUE_FIX.md`, `MVP_FREE_TEXT_20261020.md`, `CONSUMER_FIRST_UX.md`, `UX_FIRST_JOURNEY.md`, `UX_ACCESSIBILITY.md`, `UX_DESIGN_REFINEMENT.md` — отчёты о конкретных этапах, а не новые гарантии релиза. `INTENT_AGENT_REFACTOR_20261020.md` содержит **план** изменений и сведения об отдельном выполненном этапе; целевой сценарий нельзя выдавать за уже внедрённый.

При изменении API, модельных маршрутов, источника тарифов или хранилища обновляйте связанные актуальные файлы вместе с кодом. Старые результаты тестов сохраняйте с датой и не называйте новым прогоном.


[Конкурсная база](hackathon/2026-10-24/README.md) · [Полный указатель](DOCUMENTATION_INDEX.md) · [Командный установщик](TEAM_ACCESS.md).
