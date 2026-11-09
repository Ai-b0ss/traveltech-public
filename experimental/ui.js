"use strict";
const $ = (id) => document.getElementById(id),
  esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
const fmt = (value, currency = "RUB") =>
  value == null
    ? "Неизвестно"
    : new Intl.NumberFormat("ru-RU", {
        style: "currency",
        currency,
        maximumFractionDigits: 2,
      }).format(Number(value));
const date = (value) =>
  new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(value + "T00:00:00Z"));
const cityNameRu = (city) => ({
  Istanbul: "Стамбул", Dubai: "Дубай", Paris: "Париж",
  "New York": "Нью-Йорк", "Frankfurt am Main": "Франкфурт-на-Майне",
  Moscow: "Москва", Kazan: "Казань", Rome: "Рим",
  Berlin: "Берлин", Amsterdam: "Амстердам",
  Bern: "Берн", Salzburg: "Зальцбург", Turin: "Турин",
  "Saint Petersburg": "Санкт-Петербург",
})[city] || city;
const pluralAdults = (n) => {
  const a = Math.abs(n);
  return a % 10 === 1 && a % 100 !== 11 ? `${n} взрослый` : `${n} взрослых`;
};
const pluralNights = (n) => {
  const a = Math.abs(n);
  if (a % 10 === 1 && a % 100 !== 11) return `${n} ночь`;
  if (a % 10 >= 2 && a % 10 <= 4 && (a % 100 < 10 || a % 100 >= 20))
    return `${n} ночи`;
  return `${n} ночей`;
};
const pluralRooms = (n) => {
  const a = Math.abs(n);
  if (a % 10 === 1 && a % 100 !== 11) return `${n} номер`;
  if (a % 10 >= 2 && a % 10 <= 4 && (a % 100 < 10 || a % 100 >= 20))
    return `${n} номера`;
  return `${n} номеров`;
};
const pluralChildren = (n) => {
  const a = Math.abs(n);
  if (a % 10 === 1 && a % 100 !== 11) return `${n} ребёнок`;
  if (a % 10 >= 2 && a % 10 <= 4 && (a % 100 < 10 || a % 100 >= 20))
    return `${n} ребёнка`;
  return `${n} детей`;
};
const childAgeText = (a) => {
  const abs = Math.abs(a);
  if (abs % 10 === 1 && abs % 100 !== 11) return `${a} год`;
  if (abs % 10 >= 2 && abs % 10 <= 4 && (abs % 100 < 10 || abs % 100 >= 20))
    return `${a} года`;
  return `${a} лет`;
};
let state = null,
  proposal = null,
  busy = false,
  selected = new Set(),
  messages = [],
  clarification = "";
const fullTripPlan = window.traveltechTripPlanUI?.mount({ request: (path, body) => api(path, body),
  isBusy: () => busy, isDemoEnabled: () => window.fullReal === false,
  run: (callback) => action(callback), edit: (text) => {
  $("query").value = text;
  $("query").scrollIntoView({ behavior: "instant", block: "center" });
  $("query").focus({ preventScroll: true });
} });
$("full-trip-example")?.addEventListener("click", () => {
  $("query").value = "Подбери отель в Дубае с 12 по 19 ноября 2026 года, 1 номер, 2 взрослых и ребёнок 4 года, бюджет 150000 рублей. Это учебный пример Москва → Дубай.";
  $("query").scrollIntoView({ behavior: "instant", block: "center" });
  $("query").focus({ preventScroll: true });
});
const activeKey = "traveltech.plus.active",
  knownKey = "traveltech.plus.trips";
// The first render creates an in-memory draft; it has no owned server
// dialogue history until the user submits a message.
const freshDraftAtPageLoad = !sessionStorage.getItem("traveltech.plus.draft");
let draftId =
  sessionStorage.getItem("traveltech.plus.draft") ||
  crypto.randomUUID().replaceAll("-", "");
sessionStorage.setItem("traveltech.plus.draft", draftId);
window.addEventListener("traveltech-tab-identity-reset", () => {
  fullTripPlan?.update(null);
  state = null;
  proposal = null;
  messages = [];
  clarification = "";
  draftId = crypto.randomUUID().replaceAll("-", "");
  sessionStorage.setItem("traveltech.plus.draft", draftId);
});
const names = {
  city: "город",
  country: "страна",
  checkin: "заезд",
  checkout: "выезд",
  rooms: "гости и номера",
  budget: "бюджет",
  currency: "валюта",
  breakfast: "требование завтрака",
};
const errors = {
  REQUEST_DEADLINE_EXCEEDED:
    "На обработку запроса ушло слишком много времени. Автоматический повтор не выполняется; проверьте состояние запроса перед новым действием.",
  NETWORK_ERROR:
    "Сервер недоступен. Условия и текст запроса сохранены. Повторите запрос после восстановления соединения.",
  INCOMPLETE_SERVER_RESPONSE:
    "Получен неполный ответ сервера. Состояние сохранено. Автоматический повтор не выполняется.",
  STALE_REVISION:
    "Поездка уже изменилась. Обновите страницу и проверьте условия заново.",
  PREVIEW_CANCELLED: "Предложение уже отклонено. Создайте новое изменение.",
  PREVIEW_EXPIRED: "Время подтверждения истекло. Повторите запрос.",
  PREVIEW_SUPERSEDED: "Это предложение заменено новым.",
  PREVIEW_COMMITTED: "Изменение уже подтверждено.",
  LIVE_TEST_DISABLED: "Демонстрация работает офлайн. Внешние вызовы отключены.",
  BUDGET_CURRENCY_CHANGE_REQUIRES_NEW_REQUEST:
    "Валюта отличается от валюты поездки. Создайте новый запрос с полными условиями.",
};
function userErrorText(error) {
  if (String(error?.message || error).includes("GUEST_NATIONALITY_REQUIRED"))
    return "Выберите гражданство гостей над полем сообщения. Страна назначения не подставляется вместо гражданства.";
  const code = String(error?.message ?? error ?? "");
  if (error?.name === "AbortError")
    return "Ответ не получен. Проверьте сохранённое состояние запроса; автоматического повтора нет.";
  if (error instanceof SyntaxError)
    return "Файл не удалось прочитать. Выберите JSON-файл условий, сохранённый из приложения.";
  if (errors[code]) return errors[code];
  if (code === "Файл больше 1 МБ")
    return "Файл больше 1 МБ. Выберите файл условий меньшего размера.";
  if (code === "Сначала подтвердите условия и выполните отдельный поиск")
    return code + ".";
  if (
    ["Сначала подтвердите условия поездки", "Подтвердите поездку"].includes(
      code,
    )
  )
    return "Сначала подтвердите условия поездки в предпросмотре.";
  if (code === "Выберите регион")
    return "Выберите подходящий регион из найденного списка.";
  if (
    /UNKNOWN|UNRESOLVED|PRIOR_OUTCOME|NO_RETRY|ALREADY_USED|PENDING/.test(code)
  )
    return "Исход предыдущего запроса ещё не подтверждён. Проверьте его состояние; новая отправка автоматически не выполняется.";
  if (/NONCE|CLIENT_MISMATCH|NOT_OWNED|CAPABILITY/.test(code))
    return "Этой вкладке недоступна выбранная поездка или правка. Откройте поездку из своего списка.";
  if (/STALE|REVISION/.test(code))
    return "Условия поездки уже изменились. Обновите сохранённые условия перед следующим действием.";
  if (/SAVE_ONLY_CONFIRMED|TRIP_REQUIRED/.test(code))
    return "Сначала подтвердите условия поездки, затем выполните поиск для неё.";
  if (
    /PREFLIGHT|ROUTE|MODEL|CREDIT|ACCOUNT|PROVIDER|COOLDOWN|QUOTA|BALANCE/.test(
      code,
    )
  )
    return "Модель сейчас недоступна. Условия поездки сохранены. Проверьте подключение и состояние предыдущего запроса.";
  if (/SUPPLIER|SOURCE|LOCATION|FX_|UPSTREAM/.test(code))
    return "Источник не подтвердил результат. Условия поездки сохранены; наличие и цена остаются неизвестными.";
  if (/SCHEMA|SKILL_|CONTEXT|ENVELOPE|VALIDATION|CONTRACT/.test(code))
    return "Не удалось надёжно разобрать и проверить ответ. Проверьте условия поездки; автоматического повтора нет.";
  return "Не удалось выполнить действие. Условия поездки сохранены. Проверьте состояние запроса перед новым действием.";
}
function observedAtText(value) {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))?$/.test(
      value,
    )
  )
    return "Время не указано";
  const day = value.slice(0, 10),
    calendar = new Date(day + "T00:00:00Z");
  if (
    !Number.isFinite(calendar.getTime()) ||
    calendar.toISOString().slice(0, 10) !== day
  )
    return "Время не указано";
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return "Время не указано";
  return value.length === 10
    ? new Intl.DateTimeFormat("ru-RU", {
        dateStyle: "medium",
        timeZone: "UTC",
      }).format(parsed)
    : new Intl.DateTimeFormat("ru-RU", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(parsed);
}
window.traveltechErrorText = userErrorText;
window.traveltechObservedAt = observedAtText;
function notice(text, error = false) {
  $("notice").hidden = false;
  $("notice").className = error ? "error" : "";
  $("notice").textContent = text;
}
function persist() {
  try {
    sessionStorage.setItem(
      activeKey,
      JSON.stringify({
        sid: state?.session_id ?? null,
        preview_id: proposal?.preview_id ?? null,
        preview_owner: proposal?.owner_session ?? null,
        messages: messages.slice(-30),
        clarification,
      }),
    );
    if (state?.session_id) {
      const known = JSON.parse(sessionStorage.getItem(knownKey) || "[]").filter(
        (x) => x !== state.session_id,
      );
      known.unshift(state.session_id);
      sessionStorage.setItem(knownKey, JSON.stringify(known.slice(0, 30)));
    }
    // Keep the old key as a compatibility snapshot; PLUS reads active state only from this tab.
    localStorage.setItem(
      "traveltech.reference",
      sessionStorage.getItem(activeKey),
    );
  } catch {
    notice(
      "Браузер не разрешает сохранение диалога. Согласованные условия сохраняются сервером.",
      true,
    );
  }
}
async function refreshSaved() {
  const ids = JSON.parse(sessionStorage.getItem(knownKey) || "[]");
  const data = await api("/api/sessions", { ids });
  $("saved-trip").innerHTML =
    '<option value="">Выберите сохранённую поездку</option>' +
    data.sessions
      .map(
        (t) =>
          `<option value="${esc(t.session_id)}">${esc(cityNameRu(t.city))} · ${esc(date(t.checkin))} · ${esc(fmt(t.budget, t.currency))} · версия ${t.revision}</option>`,
      )
      .join("");
  if (state) $("saved-trip").value = state.session_id;
}
async function discardProposal() {
  if (!proposal) return;
  try {
    await api("/api/action", {
      action: "cancel_preview",
      sid: proposal.owner_session,
      preview_id: proposal.preview_id,
    });
  } catch (e) {
    if (
      !/^PREVIEW_(EXPIRED|CANCELLED|COMMITTED|SUPERSEDED|STALE)$/.test(
        e.message,
      )
    )
      throw e;
  }
  proposal = null;
  renderPreview();
  persist();
  window.traveltechFinishUiOperation?.();
}
function messageSource(data) {
  if (
    data.language?.source === "SKILLS_MODEL" ||
    data.agent?.extraction === "MODEL_PROPOSED_BACKEND_VALIDATED"
  ) {
    return "ИИ · условия проверены";
  }
  if (
    data.language?.executed_route === "LOCAL_RULES" ||
    data.language?.source === "LOCAL_RULES"
  )
    return "Локальная проверка";
  return "Проверка условий";
}
function addMessage(text, role = "assistant", source = "Проверка условий") {
  messages.push({ text, role, source });
  renderMessages();
  persist();
}
function renderMessages() {
  if (!messages.length) return;
  $("messages").innerHTML = messages
    .map(
      (m) =>
        `<div class="message ${m.role === "user" ? "user" : "assistant"}"><p>${esc(m.text)}</p><span>${m.role === "user" ? "Вы" : "TravelTech · " + esc(m.source || "Проверка условий")}</span></div>`,
    )
    .join("");
  $("messages").scrollTop = $("messages").scrollHeight;
}
window.traveltechObserveAgentProgress = (rid, nonce) => {
  let node = document.getElementById("agent-progress");
  if (!node) {
    node = document.createElement("p");
    node.id = "agent-progress";
    node.setAttribute("role", "status");
    node.setAttribute("aria-live", "polite");
    document.getElementById("chat-form").after(node);
  }
  const labels = {
    RUNNING: "Запрос обрабатывается",
    UNDERSTANDING: "Уточняем условия",
    VALIDATING: "Проверяем условия",
    SEARCHING: "Ищем варианты",
    COMPARING: "Сравниваем варианты",
    PREPARING_PREVIEW: "Готовим предпросмотр",
    INSPECTING: "Проверяем сохранённые сведения",
    COMPLETE: "Результат готов",
    FAILED: "Обработка завершена",
  };
  let active = true,
    timer = null,
    request = null;
  const started = Date.now();
  let stage = "RUNNING";
  const paint = () => {
    if (active) {
      node.hidden = false;
      node.textContent =
        (labels[stage] || labels.RUNNING) +
        " · " +
        Math.floor((Date.now() - started) / 1000) +
        " с";
    }
  };
  const poll = async () => {
    if (!active) return;
    paint();
    request = new AbortController();
    const limit = setTimeout(() => request?.abort(), 5000);
    try {
      const response = await fetch(
        "/api/request/status?id=" + encodeURIComponent(rid),
        { headers: { "X-TravelTech-Nonce": nonce }, signal: request.signal },
      );
      if (response.ok) {
        const data = await response.json();
        if (active && data.progress?.stage) stage = data.progress.stage;
      }
    } catch {
    } finally {
      clearTimeout(limit);
      request = null;
      if (active) {
        paint();
        timer = setTimeout(poll, 1500);
      }
    }
  };
  paint();
  timer = setTimeout(poll, 800);
  return () => {
    active = false;
    clearTimeout(timer);
    request?.abort();
    node.hidden = true;
  };
};
async function api(path, body) {
  if (body && ["/api/skills/agent", "/api/data/search"].includes(path)) {
    const nation =
      document
        .getElementById("guest-nationality")
        ?.value?.trim()
        .toUpperCase() || "";
    if (nation) body = { ...body, guest_nationality: nation };
  }
  const cfg = await (window.fullConfigPromise || Promise.resolve({}));
  const tracked =
    body &&
    cfg.nonce &&
    (path === "/api/full/agent" ||
      path === "/api/skills/agent" ||
      path.startsWith("/api/data/") ||
      path === "/api/commit" ||
      path === "/api/budget-scope" ||
      path === "/api/action");
  const demoRequest = body && ["/api/cart/demo", "/api/order/demo"].includes(path);
  if ((tracked || demoRequest) && sessionStorage.getItem("traveltech.full.pending")) {
    if (window.traveltechMvpDeterministic) {
      try {
        const previous = JSON.parse(
          sessionStorage.getItem("traveltech.full.pending") || "null",
        );
        if (
          previous?.path === "/api/skills/agent" ||
          previous?.path === "/api/full/agent"
        ) {
          await window.reconcileTravelTech?.();
        }
      } catch (_) {
        // A genuinely uncertain request is not automatically retried.
      }
    }
    if (sessionStorage.getItem("traveltech.full.pending"))
      throw new Error("UNRESOLVED_HTTP_REQUEST_CHECK_STATUS");
  }
  const rid = crypto.randomUUID().replaceAll("-", "");
  const requestHeaders = {
    "X-TravelTech-Nonce": cfg.nonce || "",
    "X-Request-ID": rid,
  };
  const controller = new AbortController(),
    timer = setTimeout(
      () => controller.abort(),
      path.startsWith("/api/data/") ||
        path === "/api/full/agent" ||
        path === "/api/skills/agent"
        ? 600000
        : path === "/api/live/sample"
          ? 145000
          : 12000,
    );
  if (tracked) {
    const pending = { request_id: rid, path };
    sessionStorage.setItem("traveltech.full.pending", JSON.stringify(pending));
    if (
      (path === "/api/full/agent" || path === "/api/skills/agent") &&
      window.traveltechPendingBackup
    ) {
      try {
        const saved = await window.traveltechPendingBackup.save(
          cfg.nonce,
          pending,
        );
        if (!saved) throw new Error("PENDING_BACKUP_UNAVAILABLE");
      } catch (_) {
        // No HTTP request has been dispatched yet. Roll back the browser
        // marker to permit an intentional retry after storage is repaired.
        sessionStorage.removeItem("traveltech.full.pending");
        throw new Error("PENDING_BACKUP_UNAVAILABLE");
      }
      pending.backup_expected = true;
      sessionStorage.setItem(
        "traveltech.full.pending",
        JSON.stringify(pending),
      );
    }
  }
  const stopProgress =
    tracked && ["/api/skills/agent", "/api/full/agent"].includes(path)
      ? window.traveltechObserveAgentProgress?.(rid, cfg.nonce)
      : null;
  try {
    const response = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: body
        ? { "Content-Type": "application/json", ...requestHeaders }
        : requestHeaders,
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    const value = await response.json();
    if (!response.ok || value?.error) {
      const failure = new Error(value?.error || "SERVER_ERROR");
      if (path === "/api/trip_plan/session") failure.details = value;
      throw failure;
    }
    if (tracked && path !== "/api/skills/agent" && path !== "/api/full/agent") {
      const pending = JSON.parse(
        sessionStorage.getItem("traveltech.full.pending") || "null",
      );
      if (pending?.request_id === rid) {
        pending.ui_response_received = true;
        sessionStorage.setItem(
          "traveltech.full.pending",
          JSON.stringify(pending),
        );
      }
    }
    return value;
  } catch (error) {
    if (error instanceof TypeError) throw new Error("NETWORK_ERROR");
    if (error instanceof SyntaxError)
      throw new Error("INCOMPLETE_SERVER_RESPONSE");
    throw error;
  } finally {
    clearTimeout(timer);
    stopProgress?.();
  }
}
window.traveltechFinishAgentUiRequest = () => {
  // HTTP DONE is not proof that browser UI applied result; preserve pending
  // across interruption for read-only HTTP journal recovery.
  const raw = sessionStorage.getItem("traveltech.full.pending");
  if (!raw) return false;
  let pending;
  try {
    pending = JSON.parse(raw);
  } catch {
    return false;
  }
  if (
    !pending ||
    !["/api/skills/agent", "/api/full/agent"].includes(pending.path)
  )
    return false;
  if (pending.backup_expected) {
    if (!window.traveltechPendingBackup?.clearIfMatches(pending.request_id))
      return false;
  }
  sessionStorage.removeItem("traveltech.full.pending");
  window.traveltechAcknowledgeHttp?.(pending.request_id);
  return true;
};
window.addEventListener("traveltech-request-recovered", (event) => {
  const value = event.detail;
  if (
    value?.preview ||
    value?.session_id ||
    value?.language_message ||
    value?.agent
  ) {
    render(value);
    addMessage(
      value.language_message ||
        "Ответ восстановлен из журнала; новая операция не отправлялась.",
    );
    notice("Сохранённый результат восстановлен. Проверьте предпросмотр.");
    window.traveltechFinishAgentUiRequest();
  }
});
let queuedMvpSupplierSearch = null;
async function action(callback) {
  if (busy) return;
  busy = true;
  $("chat-form").setAttribute("aria-busy", "true");
  $("work-status").hidden = false;
  const busyButtons = [...document.querySelectorAll("button")].filter(
    (b) =>
      !b.matches("[data-prompt],#export,#clear-compare,#chat-history-older"),
  );
  const disabledBefore = new Map(busyButtons.map((b) => [b, b.disabled]));
  busyButtons.forEach((b) => (b.disabled = true));
  $("send").textContent = "Проверяем…";
  try {
    await callback();
    window.traveltechFinishUiOperation?.();
  } catch (error) {
    const text = userErrorText(error);
    notice(text, true);
    addMessage(text);
  } finally {
    busy = false;
    $("chat-form").removeAttribute("aria-busy");
    $("work-status").hidden = true;
    for (const b of document.querySelectorAll("button"))
      if (disabledBefore.has(b)) b.disabled = disabledBefore.get(b);
    if (window.traveltechPreviewActive?.()) {
      document.querySelectorAll("[data-availability]").forEach((b) => {
        b.disabled = true;
        b.title =
          "Подтвердите или отклоните изменение условий в окне предпросмотра";
      });
    }
    if (!window.fullReal) {
      document.querySelectorAll(".current-data button").forEach((b) => {
        b.disabled = true;
        b.title = "Доступно в режиме онлайн (запуск через START-REAL.cmd)";
      });
      const df = document.getElementById("data-feedback");
      if (df && !df.textContent) {
        df.textContent =
          "Режим работы: локальный (офлайн). Поиск по архивным данным активен выше. Для вызовов к внешним API запустите START-REAL.cmd.";
      }
    }
    $("send").innerHTML = "Отправить <span>↑</span>";
    $("undo").disabled = !state?.can_undo;
    $("confirm").disabled = !proposal;
    $("cancel").disabled = !proposal;
    fullTripPlan?.refresh();
    // The commit handler runs while action() disables all buttons.
    // Schedule the one read-only supplier query only AFTER controls are
    // re-enabled. Never automatically replay an uncertain HTTP request.
    const queued = queuedMvpSupplierSearch;
    queuedMvpSupplierSearch = null;
    if (
      queued &&
      window.fullReal &&
      !proposal &&
      state?.session_id === queued.sid &&
      state?.revision === queued.revision &&
      window.traveltechPrimarySupplier === "LITEAPI" &&
      !sessionStorage.getItem("traveltech.full.pending")
    ) {
      const button = document.getElementById("live-search");
      if (button && !button.disabled) button.click();
    }
  }
}
function field(label, value, note = "") {
  return `<div class="trip-field"><span>${esc(label)}</span><b>${esc(value)}</b><small>${esc(note)}</small></div>`;
}
function renderTrip() {
  const petStatus = $("trip-constraint-status");
  petStatus.hidden = !state?.pet_constraint?.travelling_with_pet;
  petStatus.textContent = petStatus.hidden ? ""
    : "С вами питомец. Возможность проживания, доплату и тарифы нужно проверить; прежние цены больше не действуют.";
  $("trip-actions").hidden = !state?.request;
  $("history").hidden = !state?.request;
  if (!state?.request) {
    $("trip-title").textContent = "Путешествие ещё не создано";
    $("revision").textContent = "Черновик";
    $("trip-info").innerHTML =
      '<p class="muted">Опишите условия. Поездка появится здесь после подтверждения.</p>';
    return;
  }
  const r = state.request,
    nights = Math.round(
      (Date.parse(r.checkout) - Date.parse(r.checkin)) / 86400000,
    ),
    adults = r.rooms.reduce((a, x) => a + x.adults, 0);
  const childCount = r.rooms.reduce((n, x) => n + x.children.length, 0);
  $("trip-title").textContent = cityNameRu(r.city) + " · ваша поездка";
  $("revision").textContent = "Версия " + state.revision;
  $("trip-glance").textContent = new Intl.DateTimeFormat("ru-RU", {
    day: "numeric", month: "short", year: "numeric",
  }).formatRange(new Date(r.checkin + "T12:00:00"), new Date(r.checkout + "T12:00:00"))
    + " · " + pluralAdults(adults) + (childCount ? " + " + pluralChildren(childCount) : "")
    + " · Отель до " + fmt(r.budget, r.currency);
  $("trip-info").innerHTML =
    field(
      "Даты",
      date(r.checkin) + " — " + date(r.checkout),
      pluralNights(nights),
    ) +
    field(
      "Гости",
      pluralAdults(adults) +
        (childCount ? " · " + pluralChildren(childCount) : ""),
      pluralRooms(r.rooms.length) +
        (childCount
          ? " · возраст: " +
            r.rooms
              .flatMap((x) => x.children)
              .map(childAgeText)
              .join(", ")
          : ""),
    ) +
    field(
      "Бюджет отеля",
      fmt(r.budget, r.currency),
      "Только проживание. Перелёт и трансфер считаются отдельно.",
    ) +
    field(
      "Лимит всей поездки",
      state.budget_scopes?.whole_trip
        ? fmt(
            state.budget_scopes.whole_trip.amount,
            state.budget_scopes.whole_trip.currency,
          )
        : "не задан",
      state.budget_scopes?.whole_trip
        ? "На всю поездку. Полная стоимость пока неизвестна."
        : "Общий лимит не задан; бюджет отеля его не заменяет.",
    ) +
    field(
      "Завтрак",
      r.breakfast ? "Обязателен" : "Не обязателен",
      "Факты тарифов проверяются отдельно",
    );
  if (state.preferences?.length)
    $("trip-info").innerHTML +=
      '<p class="muted">Пожелания, свойства не подтверждены: ' +
      state.preferences.map((p) => esc(p.text)).join("; ") +
      "</p>";
  if (state.pet_constraint?.travelling_with_pet)
    $("trip-info").innerHTML +=
      '<p class="muted">С животным: условие подтверждено вами. Размещение — PET_POLICY_UNVERIFIED; наличие и доплата UNKNOWN. Прежние цены и корзина STALE.</p>';
  $("undo").disabled = !state.can_undo;
  $("history-items").innerHTML = state.history?.length
    ? state.history
        .map(
          (h) =>
            `<div class="history-row">Версия ${h.revision} · ${esc(cityNameRu(h.request.city))} · бюджет ${esc(fmt(h.request.budget, h.request.currency))} · ${esc(date(h.request.checkin))} — ${esc(date(h.request.checkout))}</div>`,
        )
        .join("")
    : '<p class="muted">Это исходная согласованная версия.</p>';
}
function renderPreview() {
  document.body.classList.toggle("has-proposal", Boolean(proposal));
  $("preview").hidden = !proposal;
  if (!proposal) return;
  const r = proposal.prospective_request;
  $("preview-title").textContent =
    proposal.action === "create"
      ? "Создать новую поездку?"
      : proposal.action === "scoped_budget"
        ? "Отдельный лимит всей поездки?"
        : "Изменить условия поездки?";
  if (proposal.action === "budget")
    $("preview-info").innerHTML =
      `<div class="diff"><del>${esc(fmt(state.request.budget, r.currency))}</del><span>→</span><strong>${esc(fmt(r.budget, r.currency))}</strong></div>`;
  else if (proposal.action === "refine") {
    const show = (key, value) =>
      key === "budget"
        ? fmt(value, r.currency)
        : key === "breakfast"
          ? value
            ? "Обязателен"
            : "Не обязателен"
          : key === "rooms"
            ? value
                .map(
                  (x, i) =>
                    "Номер " +
                    (i + 1) +
                    ": " +
                    pluralAdults(x.adults) +
                    (x.children.length
                      ? ", дети " + x.children.map(childAgeText).join(", ")
                      : ", без детей"),
                )
                .join("; ")
            : ["checkin", "checkout"].includes(key)
              ? date(value)
              : String(value);
    $("preview-info").innerHTML =
      '<div class="diff-grid">' +
      Object.entries(proposal.changes)
        .map(
          ([key, change]) =>
            `<div class="diff-row"><b>${esc(names[key] || key)}</b><del>${esc(show(key, change.before))}</del><span>→</span><strong>${esc(show(key, change.after))}</strong></div>`,
        )
        .join("") +
      "</div>";
  } else if (proposal.action === "create")
    $("preview-info").innerHTML =
      `<div class="preview-grid"><div>Город: <b>${esc(cityNameRu(r.city))}</b></div><div>Бюджет: <b>${esc(fmt(r.budget, r.currency))}</b></div><div>${esc(date(r.checkin))} — ${esc(date(r.checkout))}</div><div>${pluralAdults(r.rooms.reduce((n, x) => n + x.adults, 0))} · ${pluralRooms(r.rooms.length)}</div></div>`;
  else if (proposal.action === "scoped_budget")
    $("preview-info").innerHTML =
      `<p>${esc(proposal.summary || "")}</p><p class="muted">${esc(proposal.status_impact || "")}</p>`;
  else if (proposal.action === "preference")
    $("preview-info").innerHTML =
      "<p>" +
      esc(proposal.patch.text) +
      '</p><p class="muted">Это пожелание. Свойства гостиниц по нему не подтверждены.</p>';
  else
    $("preview-info").innerHTML =
      `<div class="diff"><del>${state.request.breakfast ? "Завтрак обязателен" : "Завтрак не обязателен"}</del><span>→</span><strong>${r.breakfast ? "Обязателен" : "Не обязателен"}</strong></div>`;
  $("unchanged").textContent =
    proposal.action === "create"
      ? "До подтверждения новая сессия не создаётся."
      : "Без изменений: " +
        proposal.unchanged_fields.map((k) => names[k] || k).join(", ") +
        ".";
  const pendingWishes = [
    ...(proposal.preferences || []),
    ...(proposal.additional_preferences || []),
  ];
  $("preview-impact").textContent =
    proposal.status_impact +
    (pendingWishes.length
      ? " · Не подтверждены: " + pendingWishes.join("; ")
      : "");
  $("confirm").textContent =
    proposal.action === "create"
      ? "Подтвердить поездку"
      : proposal.action === "scoped_budget"
        ? "Подтвердить отдельный лимит"
        : "Применить изменение";
}
function sourcePhotoUrl(value) {
  if (typeof value !== "string" || value.length > 2048) return null;
  try {
    const u = new URL(value);
    return u.origin === "https://i.travelapi.com" &&
      !u.username &&
      !u.password &&
      !u.search &&
      !u.hash &&
      /^\/lodging\/[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png|webp)$/i.test(
        u.pathname,
      ) &&
      !u.pathname.split("/").includes("..")
      ? u.href
      : null;
  } catch {
    return null;
  }
}
function locationMediaHtml(d) {
  const p = d.location,
    valid =
      p &&
      typeof p.latitude === "number" &&
      typeof p.longitude === "number" &&
      Number.isFinite(p.latitude) &&
      Number.isFinite(p.longitude) &&
      Math.abs(p.latitude) <= 90 &&
      Math.abs(p.longitude) <= 180;
  const photos = Array.isArray(d.photos)
    ? [...new Set(d.photos.map(sourcePhotoUrl).filter(Boolean))].slice(0, 6)
    : [];
  if (!d.address && !valid && !photos.length) return "";
  const map = valid
    ? "https://www.openstreetmap.org/?mlat=" +
      encodeURIComponent(p.latitude) +
      "&mlon=" +
      encodeURIComponent(p.longitude) +
      "#map=16/" +
      encodeURIComponent(p.latitude) +
      "/" +
      encodeURIComponent(p.longitude)
    : null;
  return (
    '<div class="hotel-location-media">' +
    (d.address
      ? "<p><b>Адрес из источника:</b> " + esc(d.address) + "</p>"
      : "") +
    (map
      ? '<a class="hotel-map-link" href="' +
        esc(map) +
        '" target="_blank" rel="noopener noreferrer">Открыть точку отеля на карте ↗</a><p class="muted">Координаты из ответа поставщика; маршрут и время в пути не рассчитаны.</p>'
      : "") +
    (photos.length
      ? '<button type="button" class="quiet" data-hotel-photos="' +
        esc(JSON.stringify(photos)) +
        '">Показать фото поставщика · ' +
        photos.length +
        '</button><p class="muted">Фото загрузятся с внешнего сервиса только по нажатию. Это фотографии отеля, без подтверждения конкретного номера.</p><div class="hotel-photo-gallery" aria-live="polite"></div>'
      : "") +
    "</div>"
  );
}
function renderGrounding(agent) {
  const box = $("agent-grounding");
  if (!box) return;
  const rows = Array.isArray(agent?.grounded_statements)
    ? agent.grounded_statements
    : [];
  const facts = agent?.facts || {};
  box.innerHTML = rows.length
    ? '<h3>Основания ответа</h3><ol class="grounding-list">' +
      rows
        .slice(0, 160)
        .map((row) => {
          const refs = (row.fact_ids || [])
            .map((id) => facts[id])
            .filter(Boolean);
          const labels = refs.map(
            (f) =>
              ({
                hotel_name: "название из источника",
                price: "цена из источника",
                reason: "условие тарифа",
                status: "проверенный статус",
              })[f.field] || "поле источника",
          );
          return (
            "<li><p>" +
            esc(row.text) +
            "</p><small>" +
            esc(
              refs.length
                ? "Основания: " + [...new Set(labels)].join(", ")
                : row.kind === "POLICY"
                  ? "Правило проверки и ограничение сервиса."
                  : "Сведения о составе и границах полученной выдачи.",
            ) +
            "</small></li>"
          );
        })
        .join("") +
      "</ol>"
    : "";
}
document.addEventListener("click", (event) => {
  const button = event.target.closest?.("[data-hotel-photos]");
  if (!button || button.disabled) return;
  const gallery = button.parentElement.querySelector(".hotel-photo-gallery");
  if (!gallery) return;
  let urls;
  try {
    urls = JSON.parse(button.dataset.hotelPhotos);
  } catch {
    return;
  }
  if (!Array.isArray(urls)) return;
  button.disabled = true;
  button.textContent = "Фотографии поставщика";
  for (const value of urls.slice(0, 6)) {
    const url = sourcePhotoUrl(value);
    if (!url) continue;
    const img = document.createElement("img");
    img.alt = "Фото отеля из ответа поставщика";
    img.loading = "lazy";
    img.decoding = "async";
    img.referrerPolicy = "no-referrer";
    img.onerror = () => {
      const message = document.createElement("p");
      message.textContent = "Фото не удалось загрузить.";
      img.replaceWith(message);
    };
    img.src = url;
    gallery.append(img);
  }
});
function hotelDetailsHtml(offer) {
  const d = offer.hotel_detail_evidence;
  if (!d) return "";
  const costs = offer.cost_evidence || {},
    fees = costs.known_excluded_by_currency || {};
  const amounts = Object.entries(fees)
    .map(([currency, amount]) => fmt(amount, currency))
    .join("; ");
  const facts = Object.entries(offer.supplier_facts || {})
    .filter(([k, v]) => v && typeof v.value === "boolean")
    .map(
      ([k, v]) =>
        (({
          pool: "Бассейн",
          parking: "Парковка",
          air_conditioning: "Кондиционер",
          quiet: "Тихие номера",
        })[k] || k) +
        ": " +
        (v.value ? "указано в источнике" : "источник указывает отсутствие"),
    );
  return (
    locationMediaHtml(d) +
    '<details class="hotel-content"><summary>Удобства, заезд и доплаты</summary>' +
    (facts.length ? "<p>" + facts.map(esc).join("; ") + "</p>" : "") +
    (d.amenity_names?.length
      ? "<p><b>Удобства по данным отеля:</b> " +
        d.amenity_names.map(esc).join("; ") +
        ".</p>"
      : "<p>Список удобств не получен.</p>") +
    (d.checkin_text ? "<p><b>Заезд:</b> " + esc(d.checkin_text) + "</p>" : "") +
    (d.checkout_text
      ? "<p><b>Выезд:</b> " + esc(d.checkout_text) + "</p>"
      : "") +
    (amounts
      ? "<p><b>Известные отдельные сборы на поездку:</b> " +
        esc(amounts) +
        ". Это сведения об отеле; они могут пересекаться со сборами в тарифе.</p>"
      : "") +
    (d.fee_disclosures?.length
      ? "<p><b>Сведения о сборах:</b> " +
        d.fee_disclosures.map(esc).join("<br>") +
        "</p>"
      : "") +
    (d.mandatory_fee_text
      ? '<p><b>Обязательные платежи и депозиты — текст поставщика:</b></p><p class="supplier-disclosure">' +
        esc(d.mandatory_fee_text) +
        "</p>"
      : "") +
    (d.optional_fee_text
      ? '<p><b>Дополнительные услуги — текст поставщика:</b></p><p class="supplier-disclosure">' +
        esc(d.optional_fee_text) +
        "</p>"
      : "") +
    (d.important_information_text
      ? "<p><b>Важные условия отеля:</b> " +
        esc(d.important_information_text) +
        "</p>"
      : "") +
    "<p>Отсутствие удобства в списке не доказывает его отсутствие. Условия конкретного номера, применимость доплат и окончательный счёт требуют проверки. Депозит не включён в стоимость проживания автоматически.</p><small>" +
    esc(d.source) +
    " · " +
    esc(observedAtText(d.observed_at)) +
    "</small></details>"
  );
}
function cardHtml(card, index) {
  const status =
    card.status === "REJECT"
      ? ["reject", "Не подходит по условиям"]
      : card.status === "REVIEW"
        ? ["review", "Требует проверки"]
        : ["met", "Соответствует тестовым условиям"];
  const archive = card.evidence === "ARCHIVED_RESPONSE",
    live = card.evidence === "PROVIDER_RESPONSE";
  const firstReason = card.reason_texts[0];
  const otherReasons = card.reason_texts.slice(1);
  // Keep the top decision visible, but don't hide the supporting evidence.
  const reasons = [
    firstReason ? `<p class="reason-primary">· ${esc(firstReason)}</p>` : "",
    otherReasons.length
      ? `<details class="hotel-reason-details">
           <summary>Другие условия · ${otherReasons.length}</summary>
           <div>${otherReasons.map((reason) => `<p>· ${esc(reason)}</p>`).join("")}</div>
         </details>`
      : "",
  ].join("");
  const mismatch = card.reasons.includes("REQUEST_CONTEXT_MISMATCH");
  const isPreview = Boolean(
    proposal?.action !== "create" && Array.isArray(proposal?.prospective_cards),
  );
  const diffBadge = isPreview
    ? '<span class="badge diff-badge">Предварительный пересчёт</span>'
    : "";
  return `<article class="hotel-card"><div class="hotel-glyph" aria-label="Источник тарифа"><span aria-hidden="true">⌂</span><small>${card.source_kind === "SANDBOX" ? "ТЕСТОВЫЙ ИСТОЧНИК" : live ? "ДАННЫЕ ПОСТАВЩИКА" : archive ? "АРХИВНЫЙ ТАРИФ" : "ТЕСТОВЫЕ ДАННЫЕ"}</small></div><div class="hotel-body"><div class="hotel-heading"><div><span class="badge ${status[0]}">${status[1]}</span>${diffBadge}<h3 class="hotel-name">${esc(card.name)}</h3><p class="hotel-room">${esc(card.room_name)}</p></div><div class="price">${esc(fmt(card.base_total, card.currency))}<small>${live ? "Наблюдаемая стоимость" : archive ? "Архивная базовая стоимость" : "Синтетическая базовая стоимость"}</small><small>${mismatch ? "Не цена для вашей поездки" : esc(card.price_basis)}</small></div></div><div class="hotel-facts"><span class="fact">Завтрак: ${card.breakfast === true ? "включён" : card.breakfast === false ? "не включён" : "неизвестно"}</span><span class="fact">Вместимость: ${card.capacity == null ? "неизвестно" : esc(card.capacity)}</span><span class="fact">Налоги: ${card.taxes_known === true ? "известны" : "не подтверждены"}</span><span class="fact">Наличие: ${card.availability_check?.availability_verified && !card.quote_change ? "наблюдалось при проверке, без брони" : "не подтверждено"}</span></div><div class="hotel-reasons">${reasons || "<p>Тестовые условия соблюдены. Реальное наличие не проверено.</p>"}</div>${hotelDetailsHtml(card)}<div class="hotel-bottom"><span>${live ? "Сохранённый ответ поставщика" : archive ? "Архив" : "Синтетический пример"} · ${esc(observedAtText(card.observed_at_or_date))}<br>Даты тарифа: ${esc(date(card.quote_checkin))} — ${esc(date(card.quote_checkout))}</span><label class="compare-toggle"><input type="checkbox" data-compare="${index}" ${selected.has(index) ? "checked" : ""}> Сравнить</label></div><details class="hotel-details"><summary>Все основания и источник</summary><p>${esc(card.price_basis)}. Полный счёт ${card.taxes_known === true ? "подтверждён тестовыми данными" : "не подтверждён"}.</p><p>${card.reason_texts.map(esc).join("<br>")}<br>${esc(card.quote_change ? "Цена или условия изменились. Требуется отдельное согласие." : "")}<br>${esc(card.source_kind === "SANDBOX" ? "Тестовая среда поставщика: реальные цена и наличие не подтверждены." : "")}<br><pre>${esc(JSON.stringify(card.supplier_facts || {}, null, 2))}</pre></p><p>Источник: ${esc(card.source_ref)}</p><p>Идентификатор тарифа: ${esc(card.identity[3])}</p><p>Связь отеля и номера: ${archive && card.metadata.identity_kind ? "Локальные ID архивной записи, не ID поставщика" : "Идентификаторы сохранённого источника"}</p><p>Срок отмены: ${card.cancel_until ? esc(card.cancel_until) : "неизвестно"}. Окончательная цена: ${card.payable_for_method ? esc(fmt(card.payable_for_method, card.currency)) : "не подтверждена"}.</p></details></div></article>`;
}
function cardsNow() {
  const cards = Array.isArray(proposal?.prospective_cards)
    ? proposal.prospective_cards
    : state?.cards || [];
  // An archive for different dates/guests is not a result for this trip.
  return cards.filter(
    (card) =>
      !(
        card.evidence === "ARCHIVED_RESPONSE" &&
        card.reasons?.includes("REQUEST_CONTEXT_MISMATCH")
      ),
  );
}
// A single, stable ordering for both the first-five list and detailed rates.
// Do not invent review scores or compare different currencies without verified FX.
function renderCards() {
  const sortPanel = $("primary-sort-controls");
  if (sortPanel) sortPanel.hidden = true;
  // Current LiteAPI results belong in the primary hotel panel. The extended
  // comparison/tools stay below; neither an archive nor a synthetic fixture
  // may masquerade as the current search.
  const observed = window.traveltechMvpSupplierView;
  if (
    !proposal && state?.session_id && observed &&
    observed.sid === state.session_id &&
    observed.revision === state.revision
  ) {
    const offers = observed.result?.offers;
    const note = $("source-note");
    const panel = $("cards");
    if (observed.pending) {
      $("result-count").textContent = "Поиск...";
      $("result-summary").textContent = "Ищем гостиницы для подтверждённой поездки.";
      note.textContent = "Отправлен запрос к LiteAPI sandbox. Результаты ещё не получены.";
      panel.innerHTML = '<p class="muted">Получаем тестовые тарифы поставщика…</p>';
      return;
    }
    if (observed.error) {
      $("result-count").textContent = "Поиск не завершён";
      $("result-summary").textContent = "Поиск поставщика не удался; условия поездки сохранены.";
      note.textContent = "Ошибка LiteAPI. Причина показана в статусе поиска. Это не означает, что отелей в городе нет.";
      panel.innerHTML = '<p class="muted">Не удалось получить тарифы. Проверьте статус поиска.</p>';
      return;
    }
    if (Array.isArray(offers)) {
      const eligible = offers.filter((o) => o?.status !== "REJECT");
      const controls = $("primary-sort-controls");
      if (controls) controls.hidden = false;
      const mode = $("primary-sort")?.value || "price";
      const visible = mvpSortOffers(eligible, mode).slice(0, 5);
      $("result-count").textContent = offers.length + " тарифов";
      $("result-summary").textContent =
        "Предварительная выдача LiteAPI · " + offers.length +
        " тарифов, не отклонено проверками: " + eligible.length +
        ". Порядок: " +
        ({price:"сначала дешевле", "price-desc":"сначала дороже",
          name:"по названию", supplier:"как вернул LiteAPI"}[mode] || "сначала дешевле") +
        ". Один отель может иметь несколько тарифов.";
      note.textContent =
        "LiteAPI sandbox. Цены тестовые, наличие и окончательная стоимость не подтверждены. Выборка поставщика может быть неполной.";
      panel.innerHTML = visible.length
        ? visible.map((o) => {
            const amount = Number(o.total);
            const price = Number.isFinite(amount) && amount >= 0
              ? new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 2}).format(amount) +
                " " + esc(o.currency || "")
              : "Стоимость не указана";
            return '<article class="hotel-card">' +
              '<div class="hotel-glyph" aria-label="Источник предложения">' +
                '<span aria-hidden="true">⌂</span><small>ТЕСТОВЫЙ ТАРИФ</small></div>' +
              '<div class="hotel-body">' +
                '<div class="hotel-heading">' +
                  '<div><h3>' + esc(o.hotel_name || "Гостиница без названия") +
                  '</h3><p class="muted">' +
                  esc(o.room_name || "Тип номера не указан") + '</p></div>' +
                  '<div class="price"><strong>' + price +
                  '</strong><small>За всю поездку, валюта поставщика</small></div>' +
                '</div>' +
                '<p class="muted">LiteAPI sandbox · наличие и окончательная цена не подтверждены</p>' +
              '</div></article>';
          }).join("")
        : '<p>Поставщик ответил, но тарифов без отклонений по заданным условиям нет.</p>';
      const button = document.createElement("button");
      button.type = "button";
      button.className = "quiet";
      button.textContent = "Все тарифы и подробные условия (" + offers.length + ") ↓";
      button.addEventListener("click", () => {
        const tools = document.getElementById("source-tools");
        if (tools) {
          tools.open = true;
          tools.scrollIntoView({behavior: "smooth", block: "start"});
        }
      });
      panel.append(button);
      return;
    }
  }
  const cards = cardsNow();
  $("result-count").textContent = cards.length + " тарифа";
  $("result-summary").textContent =
    cards.length && cards.every((c) => c.status === "REJECT")
      ? "Подходящих тарифов нет: все сохранённые варианты отклонены. Причины указаны в карточках."
      : Array.isArray(proposal?.prospective_cards)
        ? "Предварительная переоценка. Согласованные условия выше ещё не изменились."
        : state?.summary || "";
  $("source-note").textContent =
    proposal?.source_mode === "live" || state?.source_result
      ? "Сохранённое наблюдение поставщика для этих условий. Наличие и окончательная цена не подтверждены."
      : state?.scenario === "ru"
        ? "Сохранённый ответ Travel Search RU / Level.Travel. Это архив; наличие, вместимость и полный счёт требуют проверки."
        : state?.provenance?.mode === "SYNTHETIC"
          ? "Синтетический тест. Цены и свойства заданы для демонстрации, это не реальные отели."
          : state && !state?.source_result && !cards.length
            ? "Свежие тарифы ещё не получены. Поиск LiteAPI запускается после подтверждения условий."
            : state
              ? "Архив TourMind. Тарифы показаны с исходными датами, наличие не проверено."
            : "Здесь будут варианты с основаниями и неизвестными условиями.";
  const coverage = (proposal?.source_result ?? state?.source_result)?.coverage;
  if (coverage?.complete_city === false)
    $("source-note").textContent +=
      " Проверена ограниченная выборка: " +
      String(coverage.searched_hotels ?? "неизвестно") +
      " гостиниц. Это не полный поиск по городу.";
  $("cards").innerHTML = cards.length
    ? cards.map(cardHtml).join("")
    : `<div class="empty-state"><span class="empty-icon">↗</span><h3>${state?.result_status === "SOURCE_ERROR" ? "Источник не ответил" : state ? "Нет сохранённых вариантов" : "Сначала — ваши планы"}</h3><p>${state?.result_status === "SOURCE_ERROR" ? "Результат неизвестен. Условия поездки сохранены." : state ? "Для этих условий нет подходящих сохранённых тарифов. Архивные предложения для других дат или гостей исключены. Выполните поиск у поставщика." : "Опишите поездку или откройте демонстрационный пример."}</p></div>`;
  document.querySelectorAll("[data-compare]").forEach((input) =>
    input.addEventListener("change", () => {
      const index = Number(input.dataset.compare);
      if (input.checked) selected.add(index);
      else selected.delete(index);
      renderCompare();
    }),
  );
  renderCompare();
}
window.traveltechRenderMvpSupplierResults = renderCards;
$("primary-sort")?.addEventListener("change", () => {
  const detail = $("live-sort");
  if (detail) detail.value = $("primary-sort").value;
  renderCards();
  if (typeof liveRender === "function") liveRender();
});
let comparisonAnchor = null;
function sourcePoint(card) {
  const p = card.hotel_detail_evidence?.location;
  return p &&
    typeof p.latitude === "number" &&
    typeof p.longitude === "number" &&
    Number.isFinite(p.latitude) &&
    Number.isFinite(p.longitude) &&
    Math.abs(p.latitude) <= 90 &&
    Math.abs(p.longitude) <= 180
    ? p
    : null;
}
function comparisonDistance(card) {
  const p = sourcePoint(card);
  if (!comparisonAnchor || !p) return "Неизвестно";
  const rad = (x) => (x * Math.PI) / 180,
    a = comparisonAnchor;
  const v =
    Math.sin(rad(p.latitude - a.latitude) / 2) ** 2 +
    Math.cos(rad(a.latitude)) *
      Math.cos(rad(p.latitude)) *
      Math.sin(rad(p.longitude - a.longitude) / 2) ** 2;
  return (
    (
      6371 *
      2 *
      Math.atan2(Math.sqrt(Math.min(1, v)), Math.sqrt(Math.max(0, 1 - v)))
    ).toLocaleString("ru-RU", { maximumFractionDigits: 2 }) + " км по прямой"
  );
}
function updateMobileNavigation() {
  updateMobileComposer();
  const destination = $("mobile-context-link");
  if (!destination) return;

  if (proposal) {
    destination.href = "#preview";
    destination.textContent = "Подтвердить";
    return;
  }
  if (selected.size >= 2 && !$("comparison").hidden) {
    destination.href = "#comparison";
    destination.textContent = "К сравнению";
    return;
  }
  if (state?.session_id) {
    destination.href = "#cards";
    destination.textContent = "К вариантам";
    return;
  }
  destination.href = "#saved-trips";
  destination.textContent = "Мои поездки";
}

$("mobile-context-link")?.addEventListener("click", () => {
  if ($("mobile-context-link").getAttribute("href") === "#saved-trips") {
    $("saved-trips").open = true;
  }
});

// Move the same form so unsent text, handlers and request guards are preserved.
const composerHome = $("chat-form").parentElement;
const composerPlaceholder = $("query").placeholder;
const composerAnchor = document.createComment("desktop composer position");
composerHome.insertBefore(composerAnchor, $("chat-form"));
const workspaceHero = document.querySelector("main > .hero");
const workspaceNotice = $("notice");
const heroAnchor = document.createComment("initial hero position");
const noticeAnchor = document.createComment("initial notice position");
workspaceHero.before(heroAnchor);
workspaceNotice.before(noticeAnchor);
function updateMobileComposer() {
  const form = $("chat-form"), dock = document.querySelector(".mobile-navigation");
  const results = document.querySelector(".trip-results");
  if (state?.session_id && workspaceHero.parentElement !== results) {
    results.prepend(workspaceNotice);
    results.prepend(workspaceHero);
  } else if (!state?.session_id && workspaceHero.parentElement === results) {
    heroAnchor.after(workspaceHero);
    noticeAnchor.after(workspaceNotice);
  }
  const docked = Boolean(state?.session_id) && innerWidth <= 740
    && (innerHeight >= 500 || document.activeElement === $("query"));
  const workspace = $("workspace");
  const firstPanel = docked ? results : composerHome;
  if (workspace.firstElementChild !== firstPanel) workspace.prepend(firstPanel);
  if (docked && form.parentElement !== dock) dock.appendChild(form);
  else if (!docked && form.parentElement !== composerHome) composerAnchor.after(form);
  document.body.classList.toggle("composer-docked", docked);
  $("query").placeholder = docked ? "Что изменить в поездке?" : composerPlaceholder;
  $("mobile-chat-link").href = docked ? "#messages" : "#chat-form";
  if (docked) {
    const viewport = window.visualViewport;
    const keyboard = viewport ? Math.max(0, innerHeight - viewport.height - viewport.offsetTop) : 0;
    dock.style.bottom = `${keyboard}px`;
    document.documentElement.style.setProperty("--composer-dock-height", `${dock.getBoundingClientRect().height}px`);
  } else dock.style.bottom = "";
}
window.addEventListener("resize", updateMobileComposer);
window.visualViewport?.addEventListener("resize", updateMobileComposer);
window.visualViewport?.addEventListener("scroll", updateMobileComposer);

function renderCompare() {
  const cards = cardsNow(),
    items = [...selected].filter((i) => i < cards.length).map((i) => cards[i]);
  $("comparison").hidden = items.length < 2;
  updateMobileNavigation();
  if (items.length < 2) return;
  const hotelCount = new Set(
    items.map((c) => JSON.stringify(c.identity?.slice(0, 2) || [c.name])),
  ).size;
  $("compare-summary").textContent =
    "Выбрано тарифов: " +
    items.length +
    "; гостиниц: " +
    hotelCount +
    ". " +
    (hotelCount < items.length
      ? "Тарифы одной гостиницы различайте по номеру, датам, питанию и условиям отмены. "
      : "") +
    "Архивные сведения не подтверждают текущее наличие.";
  const rows = [
    ["Наблюдаемая стоимость", (c) => fmt(c.base_total, c.currency)],
    ["Номер", (c) => c.room_name || "Не указан"],
    ["Статус", (c) => (c.status === "REJECT" ? "Отклонён" : "Нужна проверка")],
    [
      "Даты тарифа",
      (c) => date(c.quote_checkin) + " — " + date(c.quote_checkout),
    ],
    [
      "Завтрак",
      (c) =>
        c.breakfast === true
          ? "Включён"
          : c.breakfast === false
            ? "Не включён"
            : "Неизвестно",
    ],
    ["Вместимость", (c) => c.capacity ?? "Неизвестно"],
    [
      "Налоги",
      (c) =>
        c.taxes_known === true ? "Подтверждены в тесте" : "Не подтверждены",
    ],
    [
      "Наличие",
      (c) =>
        c.availability_check?.availability_verified && !c.quote_change
          ? "Наблюдалось при проверке, без брони"
          : "Не подтверждено",
    ],
    ["Срок отмены", (c) => c.cancel_until || "Неизвестно"],
    [
      "Причины и ограничения",
      (c) =>
        (c.reason_texts || []).join("; ") ||
        "Нет подтверждения полного счёта и наличия",
    ],
    ["Адрес", (c) => c.hotel_detail_evidence?.address || "Не указан"],
    ["До заданной точки", comparisonDistance],
    [
      "От точки поиска поставщика",
      (c) => {
        const d = c.supplier_facts?.distance_to_search_anchor_km;
        return d?.field === "hotel.distance_km" && Number.isFinite(d.value)
          ? d.value + " км · по поставщику"
          : "Неизвестно";
      },
    ],
    ["Наблюдение", (c) => observedAtText(c.observed_at_or_date)],
  ];
  const differences = $("compare-differences").checked;
  const body = rows
    .map(([label, fn]) => {
      const values = items.map((c) => String(fn(c))),
        same = new Set(values).size === 1;
      return differences && same
        ? ""
        : '<tr class="' +
            (same ? "compare-same" : "compare-different") +
            '"><th scope="row">' +
            label +
            "</th>" +
            values
              .map((value) => {
                if (label !== "Причины и ограничения" || value.length < 120) {
                  return "<td>" + esc(value) + "</td>";
                }
                const lead = value.split(";")[0].trim();
                return `<td><p class="compare-reason-lead">${esc(lead)}</p>
                <details class="compare-reasons">
                  <summary>Все причины и ограничения</summary>
                  <p>${esc(value)}</p>
                </details></td>`;
              })
              .join("") +
            "</tr>";
    })
    .join("");
  $("compare-table").innerHTML =
    '<thead><tr><th scope="col">Критерий</th>' +
    items
      .map(
        (c) =>
          '<th scope="col">' +
          esc(c.name) +
          "<br><small>Тариф " +
          esc(String(c.identity[3]).slice(0, 12)) +
          "</small></th>",
      )
      .join("") +
    "</tr></thead><tbody>" +
    body +
    (differences
      ? ""
      : '<tr><th scope="row">Фото и карта из источника</th>' +
        items
          .map(
            (c) =>
              "<td>" +
              locationMediaHtml(c.hotel_detail_evidence || {}) +
              "</td>",
          )
          .join("") +
        "</tr>") +
    "</tbody>";
  $("compare-anchor-status").textContent = comparisonAnchor
    ? "Точка: " +
      comparisonAnchor.latitude +
      ", " +
      comparisonAnchor.longitude +
      ". Расстояние рассчитано по координатам отеля из источника, по прямой; маршрут и время в пути не рассчитаны."
    : "Задайте координаты интересующей точки, чтобы сравнить расстояние по прямой. Это не фильтр гостиничного поиска.";
}
$("compare-differences")?.addEventListener("change", renderCompare);
$("compare-anchor-apply")?.addEventListener("click", () => {
  const lat = $("compare-lat").value.trim(),
    lon = $("compare-lon").value.trim(),
    latitude = Number(lat),
    longitude = Number(lon);
  if (
    !lat ||
    !lon ||
    !Number.isFinite(latitude) ||
    !Number.isFinite(longitude) ||
    Math.abs(latitude) > 90 ||
    Math.abs(longitude) > 180
  ) {
    $("compare-anchor-status").textContent =
      "Укажите широту от −90 до 90 и долготу от −180 до 180. Предыдущая точка не изменена.";
    return;
  }
  comparisonAnchor = { latitude, longitude };
  renderCompare();
});
$("compare-anchor-clear")?.addEventListener("click", () => {
  comparisonAnchor = null;
  $("compare-lat").value = "";
  $("compare-lon").value = "";
  renderCompare();
});

function tripPlanBudgetAmount(value) {
  if (value === null) return null; // Explicitly no limit differs from absent data.
  if (!["number", "string"].includes(typeof value)) return undefined;
  const match = String(value).match(/^(\d+)(?:\.(\d{1,2}))?$/);
  return match ? `${match[1]}.${(match[2] ?? "").padEnd(2, "0")}` : undefined;
}
function render(data) {
  comparisonAnchor = null;
  if ($("compare-lat")) $("compare-lat").value = "";
  if ($("compare-lon")) $("compare-lon").value = "";
  renderGrounding(data.agent);
  $("agent-diagnostics").hidden = !data.agent;
  state = data.session_id ? data : null;
  document.body.classList.toggle("has-trip", Boolean(state?.session_id));
  proposal =
    data.preview ??
    (data.pending_preview?.has_preview ? data.pending_preview : null);
  fullTripPlan?.update(state?.request ? {
    sid: state.session_id, revision: state.revision,
    destination: cityNameRu(state.request.city),
    start_date: state.request.checkin, end_date: state.request.checkout,
    travellers: { adults: state.request.rooms.reduce((sum, room) => sum + room.adults, 0),
      children: state.request.rooms.flatMap((room) => room.children) },
    // Only saved fields may describe this session. The hotel request does not
    // guarantee origin or budget scope; never infer either from example text.
    origin: state.request.origin ?? null,
    budget: {
      amount: tripPlanBudgetAmount(state.request.budget),
      currency: state.request.currency,
      scope: state.request.budget_scope ?? null,
    },
    currency: state.request.currency, pending: Boolean(proposal),
  } : null);
  selected.clear();
  renderTrip();
  const wholeTripOffer = data.whole_trip_confirmation;
  const openWholeTrip = $("open-whole-trip");
  if (openWholeTrip) {
    const ready = Boolean(
      wholeTripOffer &&
        wholeTripOffer.applied === false &&
        wholeTripOffer.opens_preview === true &&
        wholeTripOffer.confirmation === "SEPARATE_WHOLE_TRIP" &&
        state?.session_id &&
        !proposal,
    );
    openWholeTrip.hidden = !ready;
    openWholeTrip.dataset.amount = ready ? wholeTripOffer.amount : "";
    openWholeTrip.dataset.currency = ready ? wholeTripOffer.currency : "";
    openWholeTrip.textContent = ready
      ? "Открыть отдельный лимит всей поездки " +
        wholeTripOffer.amount +
        " " +
        wholeTripOffer.currency
      : "Открыть отдельный лимит всей поездки";
  }
  renderPreview();
  renderCards();
  updateMobileNavigation();
  window.dispatchEvent(
    new CustomEvent("traveltech-agent-live-update", {
      detail: {
        result:
          proposal?.source_result ??
          data.source_result ??
          data.agent?.source_result ??
          null,
        preview: Boolean(proposal),
        canonical: Boolean(
          proposal?.source_mode === "live" || data.source_result,
        ),
        selected_offer: data.selected_offer ?? null,
      },
    }),
  );
  persist();
  window.dispatchEvent(
    new CustomEvent("traveltech-trip-rendered", {
      detail: {
        sid: state?.session_id ?? null,
        revision: state?.revision ?? null,
      },
    }),
  );
  refreshSaved().catch((e) =>
    notice("Не удалось загрузить список поездок. " + userErrorText(e), true),
  );
}
function combineClarification(prev, curr) {
  if (!prev) return curr;
  if (
    /^(?:и\s+)?(?:почему|отчего|причин|не подход|покажи|объясни|услови[яй]|правил[ао]|срок[иа]|отмен)/i.test(
      curr,
    )
  )
    return curr;
  let base = prev;
  if (/\b\d{1,2}[\s.-]+[а-я\d]+/i.test(curr) || /с\s+\d+/i.test(curr)) {
    base = base
      .replace(
        /с?\s*\b\d{1,2}(?:\s*(?:–|—|-|по|до)\s*|\s+по\s+|\s+до\s+)\d{1,2}\s+[а-я]+\s+\d{4}(?:\s*года)?/gi,
        " ",
      )
      .replace(
        /с?\s*\b\d{1,2}\s+[а-я]+(?:\s*(?:–|—|-|по|до)\s*|\s+по\s+|\s+до\s+)\d{1,2}\s+[а-я]+\s+\d{4}(?:\s*года)?/gi,
        " ",
      )
      .replace(
        /с?\s*\b\d{4}-\d{2}-\d{2}(?:\s*(?:–|—|-|по|до)\s*|\s+по\s+|\s+до\s+)\d{4}-\d{2}-\d{2}/gi,
        " ",
      )
      .replace(
        /с?\s*\b\d{1,2}\.\d{1,2}\.\d{4}(?:\s*(?:–|—|-|по|до)\s*|\s+по\s+|\s+до\s+)\d{1,2}\.\d{1,2}\.\d{4}/gi,
        " ",
      )
      .replace(/\b(?:никогдаборь|октяборь|[а-яё]+борь)\b/gi, " ");
  }
  if (/стамбул|сочи|дубай|dubai|istanbul|sochi/i.test(curr)) {
    base = base
      .replace(/в\s+[А-Яа-яA-Za-z]+(?:ул[еа]|очи|ба[еи]|бул[еа])/gi, " ")
      .replace(/\bстабмул[а-я]*\b/gi, " ");
  }
  return (base.trim() + " " + curr.trim()).trim();
}
async function submit() {
  const text = $("query").value.trim();
  if (!text) return;
  await action(async () => {
    // A fast first submission must not use offline defaults while config loads.
    if (window.fullConfigPromise) await window.fullConfigPromise;
    addMessage(text, "user");
    const isInspect =
      /^(?:и\s+)?(?:почему|отчего|причин|не подход|покажи|объясни|услови[яй]|правил[ао]|срок[иа]|отмен)/i.test(
        text.trim(),
      );
    if (proposal?.action === "create" && !state && !isInspect)
      await discardProposal();
    // A staging MVP query must never fall into an external model route,
    // even if a prior tab had AI enabled before this mode was activated.
    const agentMode =
      !window.traveltechMvpDeterministic && $("agent-mode").checked;
    const skills = agentMode && $("agent-engine")?.value === "skills";
    const combined =
      skills || !agentMode ? text : combineClarification(clarification, text);
    const data = await api(
      skills
        ? "/api/skills/agent"
        : agentMode
          ? window.fullReal
            ? "/api/full/agent"
            : "/api/agent"
          : "/api/language",
      skills
        ? {
            provider: $("provider-route").value,
            text: combined,
            sid: state?.session_id ?? null,
            revision: state?.revision ?? null,
            draft_id: draftId,
            source_mode: $("skill-source").value,
            use_jev: $("jev-advice").checked,
          }
        : agentMode
          ? {
              ...(window.fullReal
                ? { provider: $("provider-route").value, failover: true }
                : {}),
              text: combined,
              sid: state?.session_id ?? null,
              revision: state?.revision ?? null,
              draft_id: draftId,
            }
          : {
              text: combined,
              mode: "local",
              sid: state?.session_id ?? null,
              revision: state?.revision ?? null,
              preview: true,
              draft_id: draftId,
            },
    );
    $("agent-diagnostics").hidden = !data.agent;
    if (data.agent) {
      $("agent-diagnostics").hidden = false;
      $("agent-trace").textContent = JSON.stringify(
        {
          mode: data.agent.mode,
          status: data.agent.status,
          external_calls: data.agent.external_calls,
          trace_id: data.agent.trace_id,
          steps: data.agent.steps.map((x) => ({
            step: x.step,
            tool: x.tool,
            arguments: x.arguments,
            ok: x.observation?.ok,
            error: x.observation?.error,
          })),
          comparison: data.agent.comparison,
          engine: data.agent.engine,
          extraction: data.agent.extraction,
          repairs: data.agent.repairs,
          jev: data.agent.jev,
          invalidated: data.agent.invalidated,
          skill_versions: data.agent.skill_versions,
          context_metrics: data.agent.context_metrics,
          grounded_statements: data.agent.grounded_statements,
        },
        null,
        2,
      );
    }
    clarification =
      data.language?.validated_intent?.intent === "clarify" && !state
        ? combined
        : "";
    render(data);
    addMessage(data.language_message, "assistant", messageSource(data));
    notice(data.language_message);
    $("query").value = "";
    if (proposal)
      $("preview").scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "instant"
          : "smooth",
        block: "start",
      });
    window.traveltechFinishAgentUiRequest();
  });
}
$("try-free").addEventListener("click", () =>
  action(async () => {
    await discardProposal();
    clarification = "";
    try {
      const data = await api("/api/free/sample", {
        case: "free_search",
        draft_id: draftId,
      });
      render(data);
      const lang = data.language || {};
      $("live-outcome").textContent =
        "Маршрут: " +
        (lang.provider || "LOCAL") +
        "/" +
        (lang.model || "rules") +
        " · источник: " +
        (lang.source || "unknown") +
        " · вызовов моделей: " +
        (lang.model_calls ?? "?");
      addMessage(
        "Проверенный синтетический маршрут подготовил предпросмотр. Реальные гостиничные тарифы не проверены.",
      );
      $("preview").scrollIntoView({ behavior: "smooth", block: "center" });
    } finally {
      const status = await api("/api/model/status");
      if (status.remaining_requests === 0) {
        $("try-free").disabled = true;
        $("try-free").textContent = "Проверка выполнена — лимит использован";
      }
      const tr = (status.trace || [])
        .map(
          (x) =>
            x.provider +
            ": " +
            x.status +
            (x.reason ? " (" + x.reason + ")" : ""),
        )
        .join(" → ");
      if (tr && !$("live-outcome").textContent)
        $("live-outcome").textContent = tr;
    }
  }),
);
$("recover-local").addEventListener("click", () =>
  action(async () => {
    await discardProposal();
    clarification = "";
    const data = await api("/api/live/recovery", {
      case: "simulated_llm_unavailable",
      draft_id: draftId,
    });
    render(data);
    const receipt = data.language || {};
    $("live-outcome").textContent =
      "Локальное восстановление · GLM 503 (симуляция) · MiMo INVALID_JSON (симуляция) · внешних запросов 0 · " +
      (receipt.source || "нет");
    addMessage(
      "Обе модели в данном сценарии недоступны (симуляция). Независимый локальный разбор подготовил предпросмотр. Модельные ответы не использованы.",
    );
    notice(
      "Условия разобраны локально; гостиницы из архива. Подтвердите предложенные изменения отдельно.",
    );
    $("preview").scrollIntoView({ behavior: "smooth", block: "center" });
  }),
);
$("replay-mimo").addEventListener("click", () =>
  action(async () => {
    await discardProposal();
    clarification = "";
    const data = await api("/api/live/replay", {
      case: "recorded_mimo_search",
      draft_id: draftId,
    });
    render(data);
    $("live-outcome").textContent =
      "OFFLINE REPLAY исторического реального ответа MiMo · новый расход 0 · GLM 503 симулирован";
    addMessage(
      "Сохранённый ответ настоящей MiMo воспроизведён локально. Это НЕ новый вызов API. Проверьте и подтвердите предпросмотр.",
    );
    notice(
      "Источник: ранее сохранённый ответ OneAPI. Результат проверен кодом, актуальные цены неизвестны.",
    );
    $("preview").scrollIntoView({ behavior: "smooth", block: "center" });
  }),
);
$("open-ai-trip").addEventListener("click", () =>
  action(async () => {
    const data = await api("/api/live/latest");
    render(data);
    notice(
      "Открыта сохранённая поездка тестовой лаборатории. Архивная выдача не означает актуальное наличие.",
    );
  }),
);
$("try-live").addEventListener("click", () =>
  action(async () => {
    await discardProposal();
    clarification = "";
    try {
      const data = await api("/api/live/sample", {
        case: "injected_primary_outage",
        draft_id: draftId,
      });
      render(data);
      const info = data.language || {};
      const used = (info.attempts || [])
        .map((a) => a.provider + "/" + a.model + ": " + a.status)
        .join(" → ");
      $("live-outcome").textContent =
        (info.source || "НЕПОДТВЕРЖДЕНО") +
        " · " +
        (info.provider || "неизвестный провайдер") +
        "/" +
        (info.model || "неизвестная модель") +
        " · " +
        ((info.usage || {}).total_tokens ?? "?") +
        " токенов · " +
        used;
      addMessage(
        "Ответ модели сформировал предложение. Проверьте условия и подтвердите новую поездку.",
      );
      notice("Live-тест: запрос синтетический; гостиничные тарифы архивные.");
      $("preview").scrollIntoView({ behavior: "smooth", block: "center" });
    } finally {
      try {
        const st = await api("/api/model/status");
        if (st.remaining_requests === 0) {
          $("try-live").disabled = true;
          $("try-live").textContent =
            "Live-лимит использован: повторных вызовов нет";
        }
        const trace = (st.trace || [])
          .map(
            (x) =>
              x.model +
              ": " +
              x.status +
              (x.reason ? " (" + x.reason + ")" : ""),
          )
          .join(" → ");
        if (trace) {
          $("live-outcome").textContent =
            "Маршрут: " +
            trace +
            " · фактических запросов: " +
            st.attempt_count +
            " · Reformboss выключен";
        }
      } catch {}
    }
  }),
);
$("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  submit();
});
$("query").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});
document.querySelectorAll("[data-prompt]").forEach((button) =>
  button.addEventListener("click", () => {
    $("query").value = button.dataset.prompt;
    $("query").focus();
  }),
);
$("open-demo").addEventListener("click", () =>
  action(async () => {
    await discardProposal();
    clarification = "";
    render(await api("/api/action", { action: "start", name: "ru" }));
    messages = [];
    addMessage(
      "Открыта демонстрационная поездка: Сочи, 26–28 октября 2026 года, 2 взрослых, 1 номер, бюджет 30 000 ₽. Три архивных тарифа требуют проверки.",
    );
    notice("Демонстрационная поездка сохранена. Попробуйте изменить бюджет.");
    $("workspace").scrollIntoView({ behavior: "smooth", block: "start" });
  }),
);
$("new-trip").addEventListener("click", () =>
  action(async () => {
    await discardProposal();
    state = null;
    proposal = null;
    clarification = "";
    messages = [];
    draftId = crypto.randomUUID().replaceAll("-", "");
    newlyCreatedDraftId = draftId;
    sessionStorage.setItem("traveltech.plus.draft", draftId);
    render({ session_id: null });
    addMessage(
      "Опишите новую поездку. Предыдущая сессия остаётся в локальном журнале.",
    );
    // Starting a trip must not silently prefill an obsolete demo itinerary.
    $("query").value = "";
    $("query").focus();
  }),
);
$("confirm").addEventListener("click", () =>
  action(async () => {
    const data = await api("/api/commit", {
      sid: proposal.owner_session,
      preview_id: proposal.preview_id,
      revision: proposal.source_revision,
    });
    render(data);
    addMessage(data.language_message, "assistant", messageSource(data));
    // Explicit trip confirmation also authorizes one read-only sandbox search.
    // Reuse the existing search button and request journal, never retry it.
    if (
      window.fullReal &&
      window.traveltechPrimarySupplier === "LITEAPI" &&
      data.session_id &&
      !document.getElementById("agent-mode").checked
    ) {
      const tools = document.getElementById("source-tools");
      if (tools) tools.open = true;
      queuedMvpSupplierSearch = { sid: data.session_id, revision: data.revision };
    }
    notice(
      "Применено. Версия " +
        data.revision +
        "." +
        (data.can_undo
          ? " Можно отменить последнюю правку."
          : " Поездка сохранена."),
    );
  }),
);
$("cancel").addEventListener("click", () =>
  action(async () => {
    const data = await api("/api/action", {
      action: "cancel_preview",
      sid: proposal.owner_session,
      preview_id: proposal.preview_id,
    });
    render(data);
    addMessage(data.language_message, "assistant", messageSource(data));
    notice(data.language_message);
  }),
);
$("open-whole-trip")?.addEventListener("click", () =>
  action(async () => {
    const button = $("open-whole-trip");
    const data = await api("/api/budget-scope", {
      action: "confirm_separate_whole_trip",
      confirmation: "SEPARATE_WHOLE_TRIP",
      sid: state.session_id,
      revision: state.revision,
      amount: button.dataset.amount,
      currency: button.dataset.currency,
    });
    render(data);
    addMessage(data.language_message, "assistant", messageSource(data));
    notice(data.language_message);
  }),
);
$("undo").addEventListener("click", () =>
  action(async () => {
    const data = await api("/api/action", {
      action: "undo",
      sid: state.session_id,
      revision: state.revision,
    });
    render(data);
    addMessage(
      "Восстановлены предыдущие согласованные условия. Текущая версия: " +
        data.revision +
        ".",
    );
    notice("Последняя правка отменена.");
  }),
);
$("export").addEventListener("click", () => {
  if (!state) return;
  const blob = new Blob(
    [
      JSON.stringify(
        {
          schema: "TRAVELTECH_TRIP_EXPORT_V2",
          request: state.request,
          preferences: state.preferences,
          revision: state.revision,
          history: state.history,
          cards: state.cards,
          provenance: state.provenance,
        },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob),
    a = document.createElement("a");
  a.href = url;
  a.download = "TravelTech-trip.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  notice("Условия и происхождение данных экспортированы.");
});
$("open-saved").addEventListener("click", () =>
  action(async () => {
    const sid = $("saved-trip").value;
    if (!sid) {
      notice(
        $("saved-trip").options.length > 1
          ? "Выберите поездку из списка."
          : "Сохранённых поездок пока нет. Откройте пример или создайте свою поездку.",
      );
      return;
    }
    await discardProposal();
    messages = [];
    clarification = "";
    render(await api("/api/session?id=" + encodeURIComponent(sid)));
    addMessage("Открыта сохранённая поездка.");
    await loadDialogueHistory();
  }),
);
$("import-trip").addEventListener("click", () => $("import-file").click());
$("import-file").addEventListener("change", () =>
  action(async () => {
    const file = $("import-file").files[0];
    if (!file) return;
    if (file.size > 1048576) throw new Error("Файл больше 1 МБ");
    const imported = JSON.parse(await file.text());
    await discardProposal();
    const response = await api("/api/import", {
      payload: {
        request: imported.request,
        preferences: imported.preferences || [],
      },
      sid: state?.session_id ?? null,
      revision: state?.revision ?? null,
      draft_id: draftId,
    });
    proposal = response.preview;
    renderPreview();
    renderCards();
    persist();
    addMessage(response.language_message);
    $("import-file").value = "";
    $("preview").scrollIntoView({ behavior: "smooth", block: "center" });
  }),
);
$("clear-compare").addEventListener("click", () => {
  selected.clear();
  renderCards();
});
$("run-shadow").addEventListener("click", () =>
  action(async () => {
    const result = await api("/api/shadow", {
      scenario: $("failure").value,
      sid: state?.session_id ?? null,
    });
    $("shadow-result").innerHTML =
      result.steps
        .map(
          (step) =>
            `<div class="route-step"><div>${esc(step.route)}<small>${esc(step.reason)}${step.cooldown_seconds ? " · cooldown " + step.cooldown_seconds + " с" : ""}</small></div><strong>${esc(step.result)}</strong></div>`,
        )
        .join("") +
      `<div class="route-status">Выбран: <b>${esc(result.selected_route)}</b><br>Состояние сохранено: ${result.state_preserved ? "да" : "нет"} · Внешних вызовов: ${result.external_calls} · Реальная квота: не потрачена · Режим: SHADOW</div>`;
  }),
);
(async () => {
  try {
    const status = await api("/api/model/status");
    $("live-lab").hidden = !(
      (status.mode === "LIVE_SYNTHETIC_TEST" && status.real_enabled) ||
      status.mode === "REPLAY_ONLY" ||
      status.mode === "FREE_RESOURCE_LAB"
    );
    if (!$("live-lab").hidden) {
      document.querySelector(".privacy-note").textContent =
        "Обычные запросы остаются локальными. Только кнопка AI-теста отправляет заранее заданный синтетический запрос внешнему провайдеру.";
      document.querySelector(".offline").textContent =
        "AI LAB · синтетические запросы";
      $("live-status-text").textContent =
        "Подключено: " +
        (status.routes || [])
          .map((r) => r.provider + " / " + r.model)
          .join(" → ") +
        ". Попыток: " +
        (status.attempt_count ?? "?") +
        ". Осталось: " +
        (status.remaining_requests ?? "?") +
        ". Последняя: " +
        status.last_transport_status +
        ".";
      if (status.remaining_requests === 0) {
        $("try-live").disabled = true;
        $("try-live").textContent = "Лимит live-теста использован";
      }
      if (status.mode === "FREE_RESOURCE_LAB") {
        $("try-live").hidden = true;
        $("try-free").hidden = false;
        if (status.remaining_requests === 0) {
          $("try-free").disabled = true;
          $("try-free").textContent = "Лимит проверок использован";
        }
        $("live-status-text").textContent =
          "Только синтетический тест: Pollinations keyless → GLM OneAPI из бесплатного кредита → локальный разбор. Платный баланс должен быть 0,00 $.";
        document.querySelector(".offline").textContent =
          "FREE LAB · без карты · подтверждение расходов перед OneAPI";
        $("model-status").textContent =
          "Реальные запросы сторонним сервисам — только по кнопке. Обычный пользовательский текст остаётся локальным. Reformboss не вызывается.";
      }
      if (status.mode === "REPLAY_ONLY") {
        $("try-live").hidden = true;
        $("live-status-text").textContent =
          "Офлайн-демонстрация исторического ответа MiMo. Нет внешних вызовов, ключи и интернет не нужны.";
        document.querySelector(".offline").textContent =
          "ОФЛАЙН · сохранённый реальный ответ";
        $("model-status").textContent =
          "Воспроизводится сохранённый ответ MiMo; это не новый инференс.";
      }
      api("/api/live/latest")
        .then((d) => {
          $("open-ai-trip").hidden = false;
        })
        .catch(() => {});
      if (status.mode !== "REPLAY_ONLY")
        $("model-status").textContent =
          "OneAPI GLM → MiMo: резерв ограничен синтетическим тестом. Непригодный ответ модели не меняет поездку; предусмотрено локальное восстановление. Гостиницы архивные.";
    }
  } catch {}
  try {
    const saved = JSON.parse(sessionStorage.getItem(activeKey) || "null");
    if (saved) {
      messages = Array.isArray(saved.messages) ? saved.messages : [];
      clarification = saved.clarification || "";
      if (saved.sid)
        render(await api("/api/session?id=" + encodeURIComponent(saved.sid)));
      if (saved.preview_id) {
        try {
          proposal = await api(
            "/api/proposal?id=" +
              encodeURIComponent(saved.preview_id) +
              "&sid=" +
              encodeURIComponent(saved.preview_owner || ""),
          );
          renderPreview();
          renderCards();
        } catch {
          const status = await api(
            "/api/proposal/status?id=" +
              encodeURIComponent(saved.preview_id) +
              "&sid=" +
              encodeURIComponent(saved.preview_owner || ""),
          );
          if (status.lifecycle === "COMMITTED" && status.result_session_id) {
            render(
              await api(
                "/api/session?id=" +
                  encodeURIComponent(status.result_session_id),
              ),
            );
            notice(
              "Подтверждение восстановлено по журналу. Повтор операции не выполнялся.",
            );
          } else {
            proposal = null;
            renderPreview();
            persist();
          }
        }
      }
      renderMessages();
      if (state)
        notice(
          "Поездка восстановлена. Версия " +
            state.revision +
            "." +
            (proposal ? " Неподтверждённое предложение восстановлено." : ""),
        );
    }
  } catch {
    notice(
      "Сохранённая сессия недоступна. Можно открыть демонстрационную поездку.",
      true,
    );
  } finally {
    renderTrip();
    $("undo").disabled = !state?.can_undo;
    refreshSaved().catch(() => {});
    loadDialogueHistory().catch(() => {});
  }
})();

(async () => {
  try {
    const s = await api("/api/supplier/status");
    const el = $("supplier-status");
    if (el)
      el.textContent =
        s.mode === "OFFLINE_ARCHIVE"
          ? "Офлайн: используются сохранённые данные, актуальные тарифы не запрашиваются."
          : s.mode === "SANDBOX_NOT_LIVE_INVENTORY"
            ? "Тестовый режим поставщика: данные не подтверждают реальное наличие."
            : s.mode === "DISCOVERY_ONLY"
              ? "Доступен каталог отелей. Актуальные тарифы и наличие не подтверждены."
              : s.live_search_enabled
                ? "Поиск тарифов доступен. Наличие и итоговый счёт требуют отдельной проверки."
                : "Подключение к поставщику не настроено. Актуальные тарифы и наличие не проверены.";
  } catch {
    const el = $("supplier-status");
    if (el)
      el.textContent =
        "Гостиничный API недоступен: актуальная цена и наличие неизвестны.";
  }
})();

// RC20: structured inputs share the same Preview and durable request workflow.
function syncTripForm(r) {
  if (!r) return;
  for (const [id, key] of [
    ["form-city", "city"],
    ["form-country", "country"],
    ["form-checkin", "checkin"],
    ["form-checkout", "checkout"],
    ["form-budget", "budget"],
    ["form-currency", "currency"],
  ]) {
    if ($(id) && r[key] != null) $(id).value = r[key];
  }
  if (r.rooms?.length === 1) {
    const adults = String(r.rooms[0].adults),
      select = $("form-guests");
    if (select && !Array.from(select.options).some((o) => o.value === adults))
      select.add(new Option(pluralAdults(Number(adults)), adults));
    if (select) select.value = adults;
    if ($("form-children"))
      $("form-children").value = (r.rooms[0].children || []).join(", ");
  }
}
window.addEventListener("traveltech-trip-rendered", () =>
  syncTripForm(proposal?.request ?? state?.request),
);
$("form-search-btn")?.addEventListener("click", () => {
  const city = $("form-city").value.trim(),
    country = $("form-country").value.trim().toUpperCase(),
    ci = $("form-checkin").value,
    co = $("form-checkout").value,
    budget = $("form-budget").value.trim(),
    cur = $("form-currency").value;
  if (!city || !ci || !co || !budget) {
    notice(
      "Заполните город, даты и бюджет. Остальное можно уточнить в чате.",
      true,
    );
    return;
  }
  const raw = $("form-children").value.trim();
  if (raw && !/^\d{1,2}(?:\s*,\s*\d{1,2})*$/.test(raw)) {
    notice(
      "Укажите возраст каждого ребёнка через запятую, например: 7, 12.",
      true,
    );
    return;
  }
  const ages = raw ? raw.split(",").map((x) => Number(x.trim())) : [];
  if (ages.some((a) => a < 0 || a > 17)) {
    notice("Возраст детей должен быть от 0 до 17 лет.", true);
    return;
  }
  const children =
    ages.length === 1
      ? `, ребёнок ${ages[0]} лет`
      : ages.length
        ? `, дети ${ages.join(" и ")} лет`
        : ", без детей";
  clarification = "";
  $("query").value =
    `Найди отель в ${city}, ${country} с ${ci} по ${co} для ${$("form-guests").value} взрослых, 1 номер${children}, бюджет ${budget} ${cur}`;
  submit();
});
$("form-clear-btn")?.addEventListener("click", () => {
  for (const id of [
    "form-city",
    "form-checkin",
    "form-checkout",
    "form-budget",
    "form-children",
  ])
    if ($(id)) $(id).value = "";
  notice("Поля формы очищены. Согласованная поездка сохранена.");
});

// Acknowledge rendering separately from receiving HTTP; never send user text again.
window.traveltechAcknowledgeHttp = async (id) => {
  try {
    const cfg = await window.fullConfigPromise;
    const response = await fetch("/api/request/ack", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-TravelTech-Nonce": cfg.nonce,
        "X-Request-ID": crypto.randomUUID().replaceAll("-", ""),
      },
      body: JSON.stringify({ id }),
    });
    const result = await response.json();
    return response.ok && result?.acknowledged === true;
  } catch {
    return false;
  }
};
window.traveltechFinishUiOperation = () => {
  let pending;
  try {
    pending = JSON.parse(
      sessionStorage.getItem("traveltech.full.pending") || "null",
    );
  } catch {
    return false;
  }
  if (
    !pending?.ui_response_received ||
    ["/api/skills/agent", "/api/full/agent"].includes(pending.path)
  )
    return false;
  sessionStorage.removeItem("traveltech.full.pending");
  window.traveltechAcknowledgeHttp(pending.request_id);
  return true;
};
let historyBefore = null,
  historyContext = null,
  newlyCreatedDraftId = freshDraftAtPageLoad ? draftId : null;
async function loadDialogueHistory(older = false) {
  // A brand-new local draft has no server-owned dialogue history yet.
  // Requesting it before the first message would be rejected as an
  // unregistered draft and confuse the user with a meaningless 403.
  if (!state?.session_id && draftId === newlyCreatedDraftId) return;
  const context = state?.session_id
    ? "sid=" + encodeURIComponent(state.session_id)
    : "draft_id=" + encodeURIComponent(draftId);
  const cursor = older && historyContext === context ? historyBefore : null;
  if (older && !cursor) return;
  const result = await api(
    "/api/dialogue/history?" + context + (cursor ? "&before=" + cursor : ""),
  );
  const current = state?.session_id
    ? "sid=" + encodeURIComponent(state.session_id)
    : "draft_id=" + encodeURIComponent(draftId);
  if (current !== context) return;
  const restored = (result.turns || []).flatMap((t) => [
    { text: t.user_text, role: "user" },
    ...(t.assistant_text
      ? [{ text: t.assistant_text, role: "assistant" }]
      : []),
  ]);
  if (restored.length) {
    messages = older ? [...restored, ...messages] : restored;
    renderMessages();
    persist();
  }
  historyBefore = result.before;
  historyContext = context;
  let button = $("chat-history-older");
  if (!button) {
    button = document.createElement("button");
    button.id = "chat-history-older";
    button.type = "button";
    button.className = "quiet";
    button.textContent = "Предыдущие сообщения · история за 30 дней";
    button.onclick = () =>
      loadDialogueHistory(true).catch(() =>
        notice("История временно недоступна.", true),
      );
    $("messages").before(button);
  }
  button.hidden = !historyBefore;
}

/**
 * On small screens, prioritise the conversation without deleting manual
 * controls or changing their backend semantics.
 */
function setupJourneyFirstLayout() {
  const quickForm = document.getElementById("trip-quick-form");
  const savedTrips = document.getElementById("saved-trips");
  if (window.matchMedia("(max-width: 740px)").matches && quickForm) {
    quickForm.open = false;
  }
  if (savedTrips) {
    savedTrips.open = false;
  }

  // Showing an actual supplier result is a user-initiated workflow.
  // Reveal its buttons automatically, including when the source snapshot is
  // restored in another tab. Keep empty-source tools collapsed on first visit.
  const sourceTools = document.getElementById("source-tools");
  const liveCards = document.getElementById("live-cards");
  if (sourceTools && liveCards) {
    const showAvailableOffers = () => {
      const hasOffers = Boolean(liveCards.querySelector("[data-select-offer]"));
      document.body.classList.toggle("has-source-results", hasOffers);
      if (hasOffers) sourceTools.open = true;
    };
    new MutationObserver(showAvailableOffers).observe(liveCards, {
      childList: true,
      subtree: true,
    });
    showAvailableOffers();
  }

  document.getElementById("new-trip")?.addEventListener("click", () => {
    document.getElementById("workspace")?.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "instant"
        : "smooth",
      block: "start",
    });
    document.getElementById("query")?.focus({ preventScroll: true });
  });
}
setupJourneyFirstLayout();
