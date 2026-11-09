/* Main-page TripPlan reader. Session changes still belong to Preview → /api/commit. */
(function (root) {
  "use strict";
  // Source policy belongs to the caller, never to an untrusted response. No
  // MOCK source is a session quote, even when its fields happen to match.
  const endpoints = Object.freeze({
    session: { url: "/api/trip_plan/session", mode: "session", method: "POST" },
    server: { url: "/api/trip_plan/demo", mode: "fixed_demo" },
  });
  const clone = (value) => value == null ? value : structuredClone(value);
  const key = (context) => JSON.stringify(context && { ...context, pending: false });
  const sameChildren = (a, b) => Array.isArray(a) && Array.isArray(b)
    && JSON.stringify([...a].sort((x, y) => x - y)) === JSON.stringify([...b].sort((x, y) => x - y));
  const object = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const text = (v) => typeof v === "string" && Boolean(v.trim());
  const city = (v) => text(v) && /\p{L}/u.test(v);
  const currency = (v) => typeof v === "string" && /^[A-Z]{3}$/.test(v);
  const amount = (v) => typeof v === "string" && /^\d+\.\d{2}$/.test(v)
    && Number.isSafeInteger(Number(v.replace(".", "")));
  const date = (v) => typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v)
    && Number.isFinite(Date.parse(v + "T00:00:00Z"))
    && new Date(v + "T00:00:00Z").toISOString().slice(0, 10) === v;
  const time = (v) => typeof v === "string"
    && /^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(Z|[+-](0\d|1[0-4]):[0-5]\d)$/.test(v)
    && date(v.slice(0, 10)) && Number.isFinite(Date.parse(v));
  const party = (v) => object(v) && Number.isSafeInteger(v.adults) && v.adults > 0
    && Array.isArray(v.children) && v.children.every((age) => Number.isInteger(age) && age >= 0 && age < 18);
  const budget = (v) => object(v) && (v.amount === null || amount(v.amount)) && currency(v.currency)
    && ["whole_trip", "hotel_only", "unspecified"].includes(v.scope)
    && (v.amount !== null || v.scope === "unspecified");
  function validate(plan, context, mode) {
    const invalid = () => { throw Error("INVALID_TRIP_PLAN"); };
    if (!object(plan) || !text(plan.trip_id) || !Number.isSafeInteger(plan.revision) || plan.revision < 0
      || !city(plan.origin) || !city(plan.destination) || !date(plan.start_date) || !date(plan.end_date)
      || plan.end_date <= plan.start_date || !currency(plan.currency) || !party(plan.travellers)
      || !budget(plan.budget) || !(plan.total_amount === null || amount(plan.total_amount))
      || !Array.isArray(plan.warnings) || !plan.warnings.every((w) => object(w)
        && text(w.code) && ["hotel", "transport", "transfer", "insurance"].includes(w.component) && text(w.message))) invalid();
    const parts = ["hotel", "transport", "transfer", ...(Object.hasOwn(plan, "insurance") ? ["insurance"] : [])];
    for (const name of parts) {
      const part = plan[name];
      if (!object(part) || !(part.amount === null || amount(part.amount)) || !currency(part.currency)
        || !text(part.source_mode) || !text(part.provider)) invalid();
      if (part.amount === null && !plan.warnings.some((w) => w.code === "PRICE_UNKNOWN" && w.component === name)) invalid();
      if (mode === "fixed_demo" && part.source_mode !== "MOCK") invalid();
    }
    if (!date(plan.hotel.checkin) || !date(plan.hotel.checkout)
      || plan.hotel.checkin !== plan.start_date || plan.hotel.checkout !== plan.end_date
      || (Object.hasOwn(plan.hotel, "name") && !text(plan.hotel.name))
      || !text(plan.transport.kind)
      || ["departure_at", "arrival_at"].some((k) => !(time(plan.transport[k])
        || (plan.transport[k] === null && plan.transport.amount === null)))
      || ["return_departure_at", "return_arrival_at"].some((k) => Object.hasOwn(plan.transport, k)
        && plan.transport[k] !== null && !time(plan.transport[k]))
      || !text(plan.transfer.fare_scope)
      || !(plan.transfer.duration_minutes === null || (Number.isSafeInteger(plan.transfer.duration_minutes) && plan.transfer.duration_minutes >= 0))
      || !(plan.transfer.pickup_at === null || time(plan.transfer.pickup_at))
      || (Object.hasOwn(plan.transfer, "kind") && !text(plan.transfer.kind))
      || (plan.insurance?.amount === null && plan.insurance.fare_status !== "UNKNOWN")) invalid();
    const requiredUnknown = [plan.hotel, plan.transport, plan.transfer].some((p) => p.amount === null);
    if (plan.total_amount !== null && (requiredUnknown || parts.some((k) => plan[k].amount !== null && plan[k].currency !== plan.currency)
      || parts.reduce((sum, k) => sum + BigInt((plan[k].amount ?? "0.00").replace(".", "")), 0n)
        !== BigInt(plan.total_amount.replace(".", "")))) invalid();
    // Missing context is unknown, never equality between two absent fields.
    if (!date(context.start_date) || !date(context.end_date) || context.end_date <= context.start_date
      || !city(context.destination) || !party(context.travellers) || !currency(context.currency)
      || (mode === "session" && (!city(context.origin) || !budget(context.budget)
        || (context.budget.amount !== null && context.budget.scope === "unspecified"))))
      throw Error("TRIP_PLAN_CONTEXT_UNKNOWN");
    if (plan.revision !== context.revision) throw Error("STALE_REVISION");
    if (plan.destination !== context.destination || plan.start_date !== context.start_date
      || plan.end_date !== context.end_date || plan.currency !== context.currency
      || plan.travellers.adults !== context.travellers?.adults
      || !sameChildren(plan.travellers.children, context.travellers?.children))
      throw Error("TRIP_PLAN_CONTEXT_MISMATCH");
    if (mode === "session" && (plan.origin !== context.origin
      || plan.budget.amount !== context.budget.amount || plan.budget.currency !== context.budget.currency
      || plan.budget.scope !== context.budget.scope)) throw Error("TRIP_PLAN_CONTEXT_MISMATCH");
  }

  function createController({ request, onChange = () => {}, sources = endpoints, storage = root.localStorage }) {
    const policies = clone(sources);
    let context = null, plan = null, status = "idle", error = null, source = null, sourceMode = null, verification = null, serial = 0;
    let cart = null, order = null, demoBusy = false, demoError = null, questions = [];
    const snapshot = () => clone({ context, plan, status, error, source, sourceMode, verification,
      cart, order, demoBusy, demoError, questions });
    const notify = () => onChange(snapshot());
    function update(next) {
      next = text(next?.sid) && Number.isSafeInteger(next.revision) && next.revision >= 0 ? clone(next) : null;
      if (key(next) !== key(context)) {
        ++serial;
        cart = null; order = null; demoError = null;
        verification = null;
        questions = [];
        if (!next || next.sid !== context?.sid) { plan = null; source = null; sourceMode = null; }
        status = plan ? "stale" : "idle";
        error = null;
      } else if (Boolean(next?.pending) !== Boolean(context?.pending)) {
        ++serial;
        if (status === "loading") { status = plan ? "stale" : "idle"; verification = null; }
      }
      context = next;
      notify();
    }
    async function load(kind) {
      if (!context || context.pending || demoBusy || status === "loading" || !Object.hasOwn(policies, kind)) return;
      const policy = policies[kind];
      if (!object(policy) || !text(policy.url) || !["fixed_demo", "session"].includes(policy.mode)) return;
      const captured = clone(context), token = ++serial;
      if (policy.mode === "session") {
        if (sourceMode !== "session") plan = null;
        cart = null; order = null; demoError = null;
        source = kind; sourceMode = "session";
      }
      status = "loading"; error = null; verification = null; questions = []; notify();
      try {
        const result = await request(policy.url, policy.method === "POST"
          ? { sid: captured.sid, expected_revision: captured.revision } : undefined);
        if (token !== serial || key(context) !== key(captured)) return;
        if (policy.mode === "session" && result?.status === "NEEDS_INPUT")
          throw Object.assign(Error("TRIP_PLAN_CONTEXT_UNKNOWN"), { details: result });
        validate(result, captured, policy.mode);
        plan = clone(result); source = kind; sourceMode = policy.mode;
        status = policy.mode === "fixed_demo" ? "demo" : "ready";
        verification = policy.mode === "fixed_demo" ? "FIXED_MOCK_DEMO" : "VERIFIED_CONTEXT";
      } catch (failure) {
        if (token !== serial || key(context) !== key(captured)) return;
        error = ["INVALID_TRIP_PLAN", "STALE_REVISION", "TRIP_PLAN_CONTEXT_MISMATCH", "TRIP_PLAN_CONTEXT_UNKNOWN"].includes(failure?.message)
          ? failure.message : "SOURCE_UNAVAILABLE";
        if (policy.mode === "session" && failure?.details?.status === "NEEDS_INPUT") {
          error = "TRIP_PLAN_CONTEXT_UNKNOWN";
          questions = Array.isArray(failure.details.questions)
            ? failure.details.questions.filter((q) => typeof q === "string").slice(0, 8) : [];
          status = "needs_input";
        } else status = plan ? "stale" : "error";
      }
      notify();
    }
    const demoActive = () => context && !context.pending && status === "demo"
      && sourceMode === "fixed_demo" && verification === "FIXED_MOCK_DEMO";
    async function demo(kind) {
      if (!demoActive() || demoBusy || (kind === "order" && (!cart || order))) return;
      const captured = clone(context), token = serial;
      demoBusy = true; demoError = null; notify();
      try {
        const body = { sid: captured.sid, revision: captured.revision };
        if (kind === "order") {
          body.cart_id = cart.cart_id;
          // Persist BEFORE dispatch. A lost response/reload reuses this cart's key.
          const name = "traveltech.demo.order." + cart.cart_id;
          let id = storage?.getItem(name);
          if (!id) {
            id = root.crypto.randomUUID();
            storage?.setItem(name, id);
          }
          if (!id || storage?.getItem(name) !== id) throw Error("DEMO_KEY_STORAGE_REQUIRED");
          body.idempotency_key = id;
        }
        const result = await request(`/api/${kind}/demo`, body);
        if (token !== serial || key(context) !== key(captured)) return;
        if (result?.mode !== "DEMO_ONLY" || result.verification !== "FIXED_MOCK_DEMO"
          || !text(result.cart?.cart_id) || result.cart.revision !== captured.revision
          || (kind === "order" && (!text(result.order_id) || result.cart.cart_id !== cart.cart_id
            || result.payment_status !== "NOT_PROCESSED" || result.booking_status !== "NOT_BOOKED")))
          throw Error("INVALID_DEMO_RESPONSE");
        cart = clone(result.cart);
        if (kind === "order") order = clone(result);
      } catch (failure) {
        if (token !== serial || key(context) !== key(captured)) return;
        demoError = failure?.message || "DEMO_FAILED";
        if (!["NETWORK_ERROR", "INCOMPLETE_SERVER_RESPONSE", "DEMO_KEY_STORAGE_REQUIRED", "AbortError"].includes(demoError)
          && failure?.name !== "AbortError") {
          status = "stale"; verification = null;
        }
      } finally { demoBusy = false; notify(); }
    }
    return { update, load, snapshot, refresh: notify,
      createCart: () => demo("cart"), createOrder: () => demo("order") };
  }

  function mount({ request, edit, isBusy = () => false, isDemoEnabled = () => true,
    run = (callback) => callback(), sources = endpoints }) {
    const panel = document.getElementById("full-trip-plan");
    if (!panel) return { update() {} };
    const body = document.getElementById("full-trip-plan-card");
    const message = document.getElementById("full-trip-plan-status");
    const loadButtons = [...panel.querySelectorAll("[data-plan-source]")];
    const editButtons = [...panel.querySelectorAll("[data-plan-edit]")];
    const cartButton = document.getElementById("demo-cart-create");
    const orderButton = document.getElementById("demo-order-create");
    const receipt = document.getElementById("demo-cart-status");
    const errors = {
      INVALID_TRIP_PLAN: "Источник не вернул полный план. Данные не подставлены.",
      STALE_REVISION: "Источник вернул другую ревизию. Нужен новый план для текущих условий.",
      TRIP_PLAN_CONTEXT_MISMATCH: "Этот план не совпадает с отправлением, назначением, бюджетом, датами, гостями или валютой вашей поездки.",
      TRIP_PLAN_CONTEXT_UNKNOWN: "Недостаточно подтверждённых условий сессии: отправление, бюджет, даты, гости и валюты должны быть известны. План не проверен.",
      SOURCE_UNAVAILABLE: "Не удалось получить план. Попробуйте ещё раз позже; цены не подставлены из локального примера.",
    };
    const controller = createController({ request, sources, onChange(view) {
      panel.hidden = !view.context;
      panel.dataset.planStatus = view.status;
      panel.setAttribute("aria-busy", String(view.status === "loading"));
      const disabled = isBusy() || view.demoBusy || !view.context || view.context.pending || view.status === "loading";
      loadButtons.forEach((button) => { button.disabled = disabled; });
      editButtons.forEach((button) => { button.disabled = isBusy() || view.demoBusy || !view.context || view.context.pending; });
      const active = isDemoEnabled() && !disabled && view.status === "demo" && view.verification === "FIXED_MOCK_DEMO";
      if (cartButton) cartButton.disabled = !active || Boolean(view.cart);
      if (orderButton) orderButton.disabled = !active || !view.cart || Boolean(view.order);
      if (receipt) receipt.textContent = view.order
        ? `DEMO · ${view.order.order_id} · Деньги не списаны. Ничего не забронировано.`
        : view.demoError ? `DEMO · Действие не подтверждено (${view.demoError}). Деньги не списаны. Ничего не забронировано.`
        : view.demoBusy ? "DEMO · Сохраняем учебный результат…"
        : view.cart ? `DEMO · Корзина сохранена · ${view.cart.cart_id}. Фиксированный MOCK; не персональный расчёт.` : "";
      let explanation = "Выберите источник: сохранённая сессия или фиксированный учебный MOCK Москва → Дубай, 12–19 ноября 2026, 2 взрослых и ребёнок 4 лет, RUB. MOCK не пересчитывается под условия сессии.";
      if (view.status === "needs_input") explanation = "NEEDS_INPUT · План сессии не подтверждён. " + view.questions.join(" ");
      if (view.status === "demo") explanation = "MOCK · Учебный маршрут. Цены и источники показаны ниже; это не персональный расчёт и не бронирование.";
      if (view.status === "ready") explanation = "VERIFIED_CONTEXT · Поля плана совпадают с подтверждёнными условиями сессии. Режим и источник каждого компонента указаны отдельно; это не гарантия доступности или бронирования.";
      if (view.plan && view.status === "stale") explanation = "STALE · Условия изменились или план не подтверждён повторно. Ниже прежний маршрут; его цены скрыты. Актуальные условия — в блоке выше.";
      if (view.sourceMode === "fixed_demo" && view.status === "stale")
        explanation += " Повторное открытие учебного маршрута вернёт тот же MOCK, а не новые тарифы.";
      if (view.status === "loading") explanation = "Читаем источник плана…";
      if (view.context?.pending) explanation += " Изменение ещё не подтверждено; действуют прежние условия.";
      if (view.error) explanation += " " + errors[view.error];
      message.textContent = explanation;
      message.classList.toggle("full-trip-plan-warning", view.status === "demo" || view.status === "stale" || Boolean(view.error));
      panel.dataset.planVerification = view.verification ?? "UNVERIFIED";
      // Loading never grants a fresh quote: the prior plan is historical until validated.
      body.innerHTML = view.plan ? root.traveltechTripPlanCard.renderTripPlanCard(view.plan,
        { stale: !["ready", "demo"].includes(view.status), demo: view.sourceMode === "fixed_demo" }) : "";
    } });
    cartButton?.addEventListener("click", () => { if (isDemoEnabled() && !isBusy()) run(() => controller.createCart()); });
    orderButton?.addEventListener("click", () => { if (isDemoEnabled() && !isBusy()) run(() => controller.createOrder()); });
    loadButtons.forEach((button) => button.addEventListener("click", () => {
      if (!isBusy()) controller.load(button.dataset.planSource);
    }));
    editButtons.forEach((button) => button.addEventListener("click", () => {
      const current = controller.snapshot().context;
      if (isBusy() || !current || current.pending) return;
      const move = (value) => {
        const date = new Date(value + "T12:00:00Z"); date.setUTCDate(date.getUTCDate() + 2);
        return date.toISOString().slice(0, 10);
      };
      if (button.dataset.planEdit === "dates") edit(`Перенеси даты на ${move(current.start_date)} — ${move(current.end_date)}. Остальные условия оставь без изменений.`);
      else if (button.dataset.planEdit === "budget") edit("Снизь бюджет до ");
      else edit("Измени состав гостей: ");
    }));
    return controller;
  }
  const exported = { createController, mount };
  if (typeof module === "object" && module.exports) module.exports = exported;
  else root.traveltechTripPlanUI = Object.freeze(exported);
})(globalThis);
