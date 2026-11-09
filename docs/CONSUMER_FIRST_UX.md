> Исторический отчёт или проект решения. Текущий статус: [PROJECT_STATUS.md](PROJECT_STATUS.md), модели: [AI_ROUTING.md](AI_ROUTING.md). Прежние результаты и роли не означают сегодняшнюю готовность.

# Consumer-first UX: TravelTech

> Исторический UX-отчёт: порты и состояние стенда относятся ко времени проверки. Текущая архитектура: [ARCHITECTURE.md](ARCHITECTURE.md).

The primary TravelTech experience is one progressive booking-research workflow, not a mixed user/developer dashboard.

## Visible states

| State | Main content | Supporting controls |
|---|---|---|
| First visit / new draft | Intro, chat composer, example journeys | Saved trips and manual parameters are collapsed below the chat; hotel results and supplier tools are hidden |
| Unconfirmed changes | Proposal showing exactly what changes, Confirm/Cancel actions | Previously confirmed conditions stay unchanged |
| Confirmed trip | Trip facts, hotel cards, source qualifications and comparison | User can explicitly open supplier lookup and weather; engineering controls remain collapsed |
| Start a new draft | Chat returns to the primary empty state | Previously saved trips remain accessible |

A manual form and a free-text request feed the existing backend. Provider routing, model selection and failure simulation stay in a disclosure titled "Инструменты разработчика". No backend semantics, booking capability, charges or provider keys are changed by this UI update.

### Progressive disclosure implementation

`experimental/index.html` places the chat before manual forms and saved journeys in document order. `experimental/ui.js` sets `has-trip`, `has-proposal` and `has-source-results` on `document.body` only according to already-observed, real UI state. CSS controls visibility; it does **not** remove cards, fabricate prices, bypass confirmation or initiate supplier requests.

`#agent-mode`, `#provider-route`, `#expert-settings`, agent grounding and reliability simulation stay wired as before but live in the developer section. The user-facing text must not claim actual bookings or confirmed prices.

The provider attribution and licensing text remains accessible in a dedicated disclosure inside the supplier section. This does not reduce any legal license obligations.

### Verification

- Offline Edge regression: initial empty state, examples, responsive mobile and landscape layout, manual form, advanced controls, preview/commit, hotel comparison, saved-trip reload and keyboard navigation
- Exact-version backend compatibility: independent copy of the existing runtime backend with the updated static assets, on localhost; no paid provider inference
- Axe 4.10.3 WCAG automated audit of initial, advanced, confirmed hotel, supplier, comparison and preview states at 390×844, 667×375 and 1440×900
- Script evidence is stored under `verification/consumer_first/` locally, excluded from repository

Automated accessibility checks do not substitute for manual VoiceOver/NVDA testing.

## Release boundary

The public Cloudflare tunnel targets port 8815 on the Windows machine. It is **not** the isolated offline preview on port 8835 or the compatibility test on 8845. The production server must not be restarted, or its static assets replaced, until full regression and backup/rollback verification have succeeded.
