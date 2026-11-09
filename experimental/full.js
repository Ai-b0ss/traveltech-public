"use strict";
window.traveltechPendingBackup = {
  // The nonce stays in sessionStorage. The durable, origin-scoped record only
  // contains an authenticated request ID/path. Never store a credential,
  // request body or response in localStorage.
  prefix: "traveltech.full.agent-recovery.v1.",
  ttlMs: 6 * 60 * 60 * 1000,
  lastKey: null,
  valid(value) {
    return (
      typeof value === "string" && value.length >= 20 && value.length <= 256
    );
  },
  validPending(p) {
    return (
      p &&
      typeof p === "object" &&
      /^[a-f0-9]{32}$/i.test(p.request_id || "") &&
      (p.path === "/api/skills/agent" || p.path === "/api/full/agent")
    );
  },
  hex(data) {
    return [...new Uint8Array(data)]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  },
  async derive(nonce) {
    if (!this.valid(nonce) || !crypto?.subtle) return null;
    const bytes = new TextEncoder().encode(nonce);
    const hashed = await crypto.subtle.digest("SHA-256", bytes);
    const key = this.prefix + this.hex(hashed);
    const secret = await crypto.subtle.importKey(
      "raw",
      bytes,
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign", "verify"],
    );
    return { key, secret };
  },
  body(p) {
    return (
      String(p.request_id) + "|" + String(p.path) + "|" + String(p.created_at)
    );
  },
  async save(nonce, pending) {
    if (!this.validPending(pending)) return false;
    const ctx = await this.derive(nonce);
    if (!ctx) return false;
    const record = {
      request_id: pending.request_id,
      path: pending.path,
      created_at: Date.now(),
    };
    const mac = await crypto.subtle.sign(
      "HMAC",
      ctx.secret,
      new TextEncoder().encode(this.body(record)),
    );
    record.mac = this.hex(mac);
    localStorage.setItem(ctx.key, JSON.stringify(record));
    this.lastKey = ctx.key;
    return true;
  },
  async restore(nonce) {
    const ctx = await this.derive(nonce);
    if (!ctx) return null;
    this.lastKey = ctx.key;
    const raw = localStorage.getItem(ctx.key);
    if (!raw) return null;
    try {
      if (raw.length > 1500) throw Error("RECOVERY_RECORD_TOO_LARGE");
      const p = JSON.parse(raw),
        age = Date.now() - p.created_at;
      if (
        !this.validPending(p) ||
        !Number.isFinite(age) ||
        age < 0 ||
        age > this.ttlMs ||
        !/^[a-f0-9]{64}$/i.test(p.mac || "")
      )
        throw Error("INVALID_RECOVERY_RECORD");
      const bytes = new Uint8Array(
        p.mac.match(/../g).map((x) => parseInt(x, 16)),
      );
      const verified = await crypto.subtle.verify(
        "HMAC",
        ctx.secret,
        bytes,
        new TextEncoder().encode(this.body(p)),
      );
      if (!verified) throw Error("INVALID_RECOVERY_MAC");
      return { request_id: p.request_id, path: p.path };
    } catch (_) {
      localStorage.removeItem(ctx.key);
      return null;
    }
  },
  clearIfMatches(identifier) {
    const key = this.lastKey;
    if (!key || !/^[a-f0-9]{32}$/i.test(identifier || "")) return false;
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return false;
      const p = JSON.parse(raw);
      if (p.request_id !== identifier) return false;
      localStorage.removeItem(key);
      return true;
    } catch (_) {
      return false;
    }
  },
  prune() {
    try {
      const now = Date.now();
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const key = localStorage.key(i);
        if (!key?.startsWith(this.prefix)) continue;
        try {
          const raw = localStorage.getItem(key);
          if (!raw || raw.length > 1500) throw Error("CORRUPT");
          const value = JSON.parse(raw);
          if (
            !Number.isSafeInteger(value.created_at) ||
            now - value.created_at > this.ttlMs ||
            now < value.created_at
          )
            localStorage.removeItem(key);
        } catch (_) {
          localStorage.removeItem(key);
        }
      }
    } catch (_) {
      /* Browser storage can be disabled; never probe other origins. */
    }
  },
  reset() {
    this.lastKey = null;
  },
};

window.traveltechPendingBackup.prune();
window.fullConfigPromise = (async () => {
  let token = sessionStorage.getItem("traveltech.full.nonce") || "";
  // Duplicated tabs inherit sessionStorage. A live peer proves duplication;
  // reload retains the capability because the old document has closed its channel.
  const instance = crypto.randomUUID();
  if (window.BroadcastChannel) {
    const channel = new BroadcastChannel("traveltech.tab-identity");
    window.traveltechIdentityChannel = channel;
    channel.onmessage = (event) => {
      const m = event.data || {};
      if (
        m.kind === "hello" &&
        token &&
        m.token === token &&
        m.instance !== instance
      )
        channel.postMessage({ kind: "collision", target: m.instance });
      if (m.kind === "collision" && m.target === instance) {
        token = "";
        for (const key of [
          "traveltech.full.nonce",
          "traveltech.plus.active",
          "traveltech.plus.draft",
          "traveltech.full.pending",
          "traveltech.plus.trips",
        ])
          sessionStorage.removeItem(key);
        window.traveltechPendingBackup?.reset();
        window.dispatchEvent(new Event("traveltech-tab-identity-reset"));
      }
    };
    channel.postMessage({ kind: "hello", token, instance });
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  const response = await fetch("/api/config", {
    headers: { "X-TravelTech-Nonce": token },
  });
  if (!response.ok) throw new Error("CONFIG_UNAVAILABLE");
  const cfg = await response.json();
  if (cfg.nonce) {
    token = cfg.nonce;
    sessionStorage.setItem("traveltech.full.nonce", cfg.nonce);
  }
  window.fullReal = cfg.mode === "REAL_CREDIT_ONLY";
  return cfg;
})();
// function pluralAdults(n){const a=Math.abs(n);return (a%10===1&&a%100!==11)?n+' взрослый':n+' взрослых';}
// function childAgeText(age){const a=Math.abs(age);if(a%10===1&&a%100!==11)return age+' год';if(a%10>=2&&a%10<=4&&(a%100<10||a%100>=20))return age+' года';return age+' лет';}
// function pluralRooms(n){const a=Math.abs(n);if(a%10===1&&a%100!==11)return n+' номер';if(a%10>=2&&a%10<=4&&(a%100<10||a%100>=20))return n+' номера';return n+' номеров';}
function partyText(rooms) {
  return rooms
    .map(
      (r, i) =>
        "Номер " +
        (i + 1) +
        ": " +
        pluralAdults(r.adults) +
        (r.children?.length
          ? ", дети " + r.children.map(childAgeText).join(", ")
          : ", без детей"),
    )
    .join("; ");
}
function showDataSummary(result) {
  const box = document.getElementById("data-summary");
  if (!box) return;
  if (result.daily) {
    box.innerHTML =
      "<p>Open-Meteo.com · прогноз получен " +
      esc(
        window.traveltechObservedAt?.(result.observed_at) || "Время не указано",
      ) +
      "</p><table><thead><tr><th>Дата</th><th>Температура, °C</th><th>Осадки, %</th></tr></thead><tbody>" +
      result.daily.time
        .map(
          (day, i) =>
            "<tr><td>" +
            esc(day) +
            "</td><td>" +
            esc(result.daily.temperature_2m_min[i]) +
            "…" +
            esc(result.daily.temperature_2m_max[i]) +
            "</td><td>" +
            esc(result.daily.precipitation_probability_max[i]) +
            "</td></tr>",
        )
        .join("") +
      "</tbody></table><p>Прогноз может измениться. " +
      esc(result.attribution) +
      "</p>";
  } else if (result.checked_rates) {
    box.innerHTML =
      "<p>Проверка TourMind · " +
      esc(
        window.traveltechObservedAt?.(result.observed_at) || "Время не указано",
      ) +
      " · " +
      (result.availability_verified
        ? "Поставщик вернул доступный тариф на момент ответа."
        : "Наличие тарифа не установлено.") +
      "</p>" +
      result.checked_rates
        .map(
          (r) =>
            "<p>Цена из ответа (не итоговая): " +
            esc(r.total) +
            " " +
            esc(r.currency) +
            " за всё проживание; доступных номеров в ответе: " +
            esc(r.allotment ?? "неизвестно") +
            ". " +
            (r.room_name_consistent
              ? ""
              : "Название номера у поставщика изменилось: " +
                esc(r.room_name) +
                ". Требует сверки.") +
            "</p>",
        )
        .join("") +
      "<p>Бронирование не выполнено. Налоги и окончательный счёт требуют проверки; доступность может измениться.</p>";
  } else if (result.features) {
    box.innerHTML =
      "<p>Только места и координаты; без цен и наличия гостиниц.</p><ul>" +
      result.features
        .map(
          (f) =>
            "<li>" +
            esc(f.properties?.name || f.properties?.city || "Место") +
            " · " +
            esc(f.properties?.country || "") +
            " · " +
            esc(f.geometry?.coordinates?.join(", ")) +
            "</li>",
        )
        .join("") +
      "</ul><p>" +
      esc(result.attribution) +
      "</p>";
  } else if (result.forecast_available === false) {
    box.textContent =
      "Для указанных дат прогноз недоступен: горизонт до 16 дней.";
  } else box.textContent = "";
}
let liveLocation = null,
  liveResult = null,
  liveSelected = new Set(),
  liveLimit = 20,
  liveSelection = null;
let traveltechLiveViewSerial = 0;
function invalidateLiveView() {
  traveltechLiveViewSerial++;
  liveResult = null;
  liveSelected.clear();
  liveSelection = null;
  liveLimit = 20;
  liveRender();
}
window.reconcileTravelTech = async () => {
  let pending = JSON.parse(
    sessionStorage.getItem("traveltech.full.pending") || "null",
  );
  const cfg = await window.fullConfigPromise;
  if (!pending && window.traveltechPendingBackup) {
    pending = await window.traveltechPendingBackup.restore(cfg.nonce);
    if (pending) {
      pending.backup_expected = true;
      sessionStorage.setItem(
        "traveltech.full.pending",
        JSON.stringify(pending),
      );
    }
  }
  if (!pending) return { state: "NONE" };
  if (pending.backup_expected) {
    // A reload keeps sessionStorage but resets this script's cached identity.
    // Reauthenticate the durable mirror before applying/clearing a UI receipt.
    const authenticated = await window.traveltechPendingBackup?.restore(
      cfg.nonce,
    );
    if (
      !authenticated ||
      authenticated.request_id !== pending.request_id ||
      authenticated.path !== pending.path
    ) {
      dataFeedback(
        "Не удалось подтвердить владельца незавершённого запроса. Повтор запрещён; проверьте состояние вручную.",
        true,
      );
      return { state: "RECOVERY_IDENTITY_UNVERIFIED", redispatch: false };
    }
  }
  const response = await fetch(
    "/api/request/status?id=" + encodeURIComponent(pending.request_id),
    {
      headers: { "X-TravelTech-Nonce": cfg.nonce },
    },
  );
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "REQUEST_STATUS_FAILED");
  if (
    value.effect === "NONE" &&
    (value.state === "ABORTED_NO_EFFECT" ||
      (value.state === "DONE" && value.code !== 200)) &&
    (pending.path === "/api/commit" || pending.path === "/api/action")
  ) {
    sessionStorage.removeItem("traveltech.full.pending");
    dataFeedback(
      "\u041b\u043e\u043a\u0430\u043b\u044c\u043d\u0430\u044f \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u044f \u043d\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u0438\u043b\u0430\u0441\u044c: \u0431\u0430\u0437\u0430 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u043b\u0430 \u043e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0438\u0435 \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0439. \u041c\u043e\u0436\u043d\u043e \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u043d\u043e\u0432\u044b\u0439 \u0437\u0430\u043f\u0440\u043e\u0441 \u0432\u0440\u0443\u0447\u043d\u0443\u044e.",
      true,
    );
    return value;
  }
  // An immutable DONE/error receipt is not an in-flight request.
  // Do not replay it; permit an independent local MVP itinerary even when
  // the outcome of the OLD model call remains unknown. The old result is
  // still recorded in the server journal.
  if (
    value.state === "DONE" &&
    value.code !== 200 &&
    (pending.path === "/api/skills/agent" ||
      pending.path === "/api/full/agent") &&
    window.traveltechMvpDeterministic
  ) {
    if (
      !pending.backup_expected ||
      window.traveltechPendingBackup?.clearIfMatches(pending.request_id)
    ) {
      sessionStorage.removeItem("traveltech.full.pending");
      window.traveltechAcknowledgeHttp?.(pending.request_id);
      dataFeedback(
        "Старый запрос ИИ завершился ошибкой. Его исход сохранён в журнале и не повторяется. " +
          "Можно начать новую поездку в локальном режиме MVP.",
        true,
      );
      return { ...value, recovered_error: true, old_model_outcome_unknown: true };
    }
  }
  const knownFailure =
    (value.state === "BODY_EXPIRED" ||
      (value.state === "DONE" && value.code !== 200)) &&
    (value.recovery?.safe_to_continue === true ||
      value.response?.state_changed === false) &&
    !value.recovery?.owner_in_flight;
  if (knownFailure) {
    if (
      pending.path === "/api/skills/agent" ||
      pending.path === "/api/full/agent"
    ) {
      if (!window.traveltechFinishAgentUiRequest?.())
        return { ...value, ui_applied: false };
    } else sessionStorage.removeItem("traveltech.full.pending");
    window.traveltechAcknowledgeHttp?.(pending.request_id);
    dataFeedback(
      value.state === "BODY_EXPIRED"
        ? "Срок хранения ответа истёк. Откройте сохранённую поездку; старый запрос не повторялся."
        : "Предыдущий запрос завершился ошибкой. Его результат сохранён; повтор не отправлялся. Можно отправить новое условие.",
      true,
    );
    return { ...value, recovered_error: true };
  }
  if (value.state !== "DONE") {
    dataFeedback(
      "\u0417\u0430\u043f\u0440\u043e\u0441 \u0435\u0449\u0451 PENDING \u0438\u043b\u0438 \u0435\u0433\u043e \u0438\u0441\u0445\u043e\u0434 \u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u0435\u043d. \u041d\u043e\u0432\u0430\u044f \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0430 \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u0430; \u043f\u043e\u0432\u0442\u043e\u0440 \u043d\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f.",
      true,
    );
    return value;
  }
  const completed =
    value.code === 200 &&
    value.response &&
    typeof value.response === "object" &&
    !value.response.error;
  if (!completed) {
    dataFeedback(
      "\u0416\u0443\u0440\u043d\u0430\u043b \u0441\u043e\u0445\u0440\u0430\u043d\u0438\u043b \u043e\u0448\u0438\u0431\u043a\u0443: " +
        (window.traveltechErrorText?.(
          new Error(value.response?.error || String(value.code)),
        ) || "Действие завершилось ошибкой.") +
        ". \u0418\u0441\u0445\u043e\u0434 \u0432\u043d\u0435\u0448\u043d\u0435\u0433\u043e \u0437\u0430\u043f\u0440\u043e\u0441\u0430 \u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d; \u043d\u043e\u0432\u0430\u044f \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0430 \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u0430.",
      true,
    );
    return value;
  }
  const result = value.response;
  const path = String(pending.path || "");
  if (path === "/api/skills/agent" || path === "/api/full/agent") {
    // Reconciliation emits the saved server response without redispatch.
    // UI listener removes pending ONLY if it successfully applies the result.
    window.dispatchEvent(
      new CustomEvent("traveltech-request-recovered", { detail: result }),
    );
    if (sessionStorage.getItem("traveltech.full.pending")) {
      dataFeedback(
        "Ответ из журнала получен, но не применён к интерфейсу. Повтор запрещён; проверьте результат вручную.",
        true,
      );
      return { ...value, ui_applied: false };
    }
    return { ...value, ui_applied: true };
  }
  if (path.startsWith("/api/data/")) {
    const details = document.getElementById("data-details");
    if (details) details.textContent = JSON.stringify(result, null, 2);
    if (path === "/api/data/search") {
      if (
        state &&
        typeof state.session_id === "string" &&
        state.session_id === result.session_id &&
        typeof state.revision === "number" &&
        state.revision === result.revision &&
        Array.isArray(result.offers)
      ) {
        liveResult = result;
        liveSelected.clear();
        liveLimit = 20;
        liveRender();
        dataFeedback(
          "\u0412\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0430 \u0432\u044b\u0434\u0430\u0447\u0430 \u0433\u043e\u0441\u0442\u0438\u043d\u0438\u0447\u043d\u043e\u0433\u043e \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u0430 \u0434\u043b\u044f \u0442\u0435\u043a\u0443\u0449\u0435\u0439 \u043f\u043e\u0435\u0437\u0434\u043a\u0438.",
        );
      } else {
        dataFeedback(
          "\u041e\u0442\u0432\u0435\u0442 \u0433\u043e\u0441\u0442\u0438\u043d\u0438\u0447\u043d\u043e\u0433\u043e \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u0430 \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d \u0432 \u0436\u0443\u0440\u043d\u0430\u043b\u0435, \u043d\u043e \u043d\u0435 \u0441\u043e\u0432\u043f\u0430\u0434\u0430\u0435\u0442 \u0441 \u0442\u0435\u043a\u0443\u0449\u0435\u0439 \u043f\u043e\u0435\u0437\u0434\u043a\u043e\u0439. \u0412\u044b\u0434\u0430\u0447\u0430 \u043d\u0435 \u043f\u043e\u0434\u043c\u0435\u043d\u044f\u043b\u0430\u0441\u044c.",
          true,
        );
      }
    } else if (
      path === "/api/data/weather" ||
      path === "/api/data/availability" ||
      path === "/api/data/geo"
    ) {
      showDataSummary(result);
      dataFeedback(
        "\u041e\u0442\u0432\u0435\u0442 \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u0430 \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d \u0438\u0437 \u0436\u0443\u0440\u043d\u0430\u043b\u0430; \u043f\u043e\u0432\u0442\u043e\u0440 \u043d\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u043b\u0441\u044f.",
      );
    } else
      dataFeedback(
        "\u041e\u0442\u0432\u0435\u0442 \u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a\u0430 \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d \u0438\u0437 \u0436\u0443\u0440\u043d\u0430\u043b\u0430; \u043f\u043e\u0432\u0442\u043e\u0440 \u043d\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u043b\u0441\u044f.",
      );
  } else if (
    result.preview ||
    (result.session_id && result.request && typeof result.revision === "number")
  ) {
    window.dispatchEvent(
      new CustomEvent("traveltech-request-recovered", { detail: result }),
    );
  } else {
    dataFeedback(
      "\u0417\u0430\u043f\u0440\u043e\u0441 \u0437\u0430\u0432\u0435\u0440\u0448\u0451\u043d, \u043d\u043e \u043e\u0442\u0432\u0435\u0442 \u043d\u0435 \u0441\u043e\u0434\u0435\u0440\u0436\u0438\u0442 \u043f\u043e\u043b\u043d\u043e\u0433\u043e \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u044f \u043f\u043e\u0435\u0437\u0434\u043a\u0438. \u0421\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435 \u043d\u0435 \u043f\u043e\u0434\u043c\u0435\u043d\u044f\u043b\u043e\u0441\u044c.",
      true,
    );
  }
  sessionStorage.removeItem("traveltech.full.pending");
  window.traveltechAcknowledgeHttp?.(pending.request_id);
  return value;
};
document.addEventListener("DOMContentLoaded", () => {
  const button = document.createElement("button");
  button.id = "reconcile-request";
  button.type = "button";
  button.className = "quiet";
  button.textContent = "Проверить исход последнего запроса · без повтора";
  button.addEventListener("click", async () => {
    try {
      const result = await window.reconcileTravelTech();
      if (result.state === "NONE")
        dataFeedback("Незавершённых HTTP-запросов этой вкладки нет.");
    } catch (e) {
      dataFeedback(
        window.traveltechErrorText?.(e) ||
          "Исход пока неизвестен. Повтор не отправлен.",
        true,
      );
    }
  });
  document.querySelector(".route-controls").append(button);
  if (sessionStorage.getItem("traveltech.full.pending"))
    dataFeedback(
      "Сохранён незавершённый запрос. Проверьте его исход кнопкой у выбора провайдера; новая генерация заблокирована.",
      true,
    );
});
function dataFeedback(text, error = false) {
  document.getElementById("data-feedback").textContent = text;
  document.getElementById("data-feedback").className = error ? "error" : "";
}
async function dataAction(task) {
  const buttons = [...document.querySelectorAll(".current-data button")];
  const prior = buttons.map((b) => b.disabled);
  buttons.forEach((b) => (b.disabled = true));
  dataFeedback(
    "Запрос к источнику выполняется. Повтор автоматически не отправляется.",
  );
  try {
    await task();
    window.traveltechFinishUiOperation?.();
  } catch (e) {
    dataFeedback(
      window.traveltechErrorText?.(e) ||
        "Источник недоступен. Согласованные условия сохранены.",
      true,
    );
  } finally {
    buttons.forEach((b, i) => (b.disabled = prior[i] || !window.fullReal));
    if (liveResult?.__agentPreview)
      document
        .querySelectorAll("[data-availability]")
        .forEach((b) => (b.disabled = true));
  }
}
function chosenLocation() {
  if (!liveLocation) throw new Error("Сначала найдите локацию");
  if (document.getElementById("nearby-mode").checked) {
    if (!liveLocation.place) throw new Error("Точная точка не найдена");
    return {
      mode: "nearby",
      latitude: liveLocation.place.latitude,
      longitude: liveLocation.place.longitude,
      radius_km: Number(document.getElementById("nearby-radius").value),
      location_name: liveLocation.place.name,
    };
  }
  const choice = document.getElementById("location-choice").value;
  if (choice === "") throw new Error("Выберите регион");
  const region = liveLocation.regions[Number(choice)];
  if (!region) throw new Error("Выберите регион");
  return {
    mode: "region",
    region_id: String(region.region_id),
    location_name: region.full_name || region.name,
  };
}

const statusName = (o) =>
  o.status === "REJECT"
    ? "Не подходит"
    : o.status === "CONSTRAINTS_MET_SNAPSHOT"
      ? "Условия снимка совпали"
      : "Требует проверки";
const reasonName = {
  TAX_COVERAGE_UNVERIFIED: "Налоги и сборы не подтверждены",
  CAPACITY_UNKNOWN: "Вместимость не подтверждена",
  AVAILABILITY_NOT_CHECKED: "Наличие не проверено",
  PRICE_UNKNOWN: "Цена неизвестна",
  FX_NOT_AVAILABLE: "Подтверждённого курса нет",
  BASE_PRICE_OVER_BUDGET: "Базовая цена превышает бюджет",
  BREAKFAST_UNKNOWN: "Завтрак не подтверждён",
  BREAKFAST_NOT_INCLUDED: "Завтрак не включён",
  STALE_SOURCE: "Снимок устарел",
  REQUEST_CONTEXT_MISMATCH: "Тариф не совпадает с условиями поездки",
  ROOM_COUNT_AVAILABILITY_UNKNOWN: "Наличие всех номеров не проверено",
  SUPPLIER_CONFIRMATION_REQUIRED: "Необходимо подтверждение поставщика",
  INVALID_PRICE: "Некорректная цена",
  CANCELLATION_UNKNOWN: "Условия отмены не подтверждены",
  CAPACITY_EXCEEDED: "Недостаточная вместимость",
};
function quoteMoney(value, currency) {
  if (value == null || value === "") return "Стоимость неизвестна";
  const num = Number(value);
  if (!Number.isFinite(num) || num < 0) return "Стоимость неизвестна";
  const cur = String(currency || "").toUpperCase();
  if (!/^[A-Z]{3}$/.test(cur)) return String(value) + " " + cur;
  try {
    return new Intl.NumberFormat("ru-RU", {
      style: "currency",
      currency: cur,
      maximumFractionDigits: 2,
    }).format(num);
  } catch {
    return String(value) + " " + cur;
  }
}
function fxQuote(o) {
  const target = liveResult?.request?.currency;
  if (o.currency === target)
    return {
      amount: Number(o.total),
      currency: target,
      comparable: Number.isFinite(Number(o.total)),
    };
  if (
    o.currency === "CNY" &&
    o.fx?.verified === true &&
    o.fx?.rates?.[target] &&
    o.approximate_budget_total != null &&
    Number.isFinite(Number(o.approximate_budget_total)) &&
    Number(o.approximate_budget_total) >= 0
  )
    return {
      amount: Number(o.approximate_budget_total),
      currency: target,
      comparable: true,
      approximate: true,
      source: o.fx.source,
      updated: o.fx.data_updated_at,
    };
  return { amount: null, currency: target, comparable: false };
}
function livePriceInfo(o) {
  const native = quoteMoney(o.total, o.currency),
    converted = fxQuote(o);
  const text = converted.approximate
    ? "≈ " +
      quoteMoney(converted.amount, converted.currency) +
      " · справочный курс, не цена оплаты"
    : o.currency !== converted.currency
      ? "Без подтверждённого пересчёта в " +
        String(converted.currency || "валюту бюджета")
      : "Стоимость без гарантии окончательного счёта";
  return (
    '<div class="live-price"><strong>' +
    esc(native) +
    "</strong><span>" +
    esc(text) +
    "</span></div>"
  );
}
function liveSortKey(a, b) {
  const ca = fxQuote(a),
    cb = fxQuote(b);
  // Never compare original prices in unlike currencies without verified FX.
  if (ca.comparable !== cb.comparable) return ca.comparable ? -1 : 1;
  if (ca.comparable && cb.comparable) return ca.amount - cb.amount;
  if (a.currency !== b.currency)
    return String(a.currency).localeCompare(String(b.currency));
  return Number(a.total ?? Infinity) - Number(b.total ?? Infinity);
}
function liveOfferCard(o) {
  const reasons = Array.isArray(o.reasons) ? o.reasons : [];
  const converted = fxQuote(o);
  const status = o.status === "REJECT" ? "reject" : "review";
  const cancelled =
    o.cancellation && typeof o.cancellation === "object"
      ? o.cancellation
      : null;
  const refundable =
    cancelled?.effective_non_refundable === true
      ? "Невозвратный тариф"
      : cancelled?.free_cancel_deadline
        ? "До " + String(cancelled.free_cancel_deadline)
        : "Условия отмены неизвестны";
  const supplier = o.supplier_facts || {};
  const stars =
    supplier.star_rating?.field === "hotel.star_rating" &&
    Number.isInteger(supplier.star_rating.value)
      ? supplier.star_rating.value
      : null;
  const distance =
    supplier.distance_to_search_anchor_km?.field === "hotel.distance_km" &&
    typeof supplier.distance_to_search_anchor_km.value === "number"
      ? supplier.distance_to_search_anchor_km.value
      : null;
  const features = [
    ...(stars !== null
      ? [["Звёзды по поставщику", String(stars) + " из 5"]]
      : []),
    ...(distance !== null
      ? [["От точки поиска", String(distance) + " км · по поставщику"]]
      : []),
    [
      "Завтрак",
      o.breakfast === true
        ? "Включён"
        : o.breakfast === false
          ? "Не включён"
          : "Неизвестно",
    ],
    ["Вместимость", o.capacity == null ? "Неизвестно" : String(o.capacity)],
    [
      "Налоги",
      o.taxes_known === true ? "Подтверждены в источнике" : "Не подтверждены",
    ],
    [
      "Наличие",
      o.availability_verified === true
        ? "Проверено на момент ответа"
        : "Не проверено",
    ],
  ];
  const checkDisabled = liveResult?.__agentPreview === true;
  const saved =
    liveSelection?.source_snapshot_id === liveResult?.source_snapshot_id &&
    liveSelection?.hotel_id === o.hotel_id &&
    liveSelection?.room_id === o.room_id &&
    liveSelection?.rate_code === o.rate_code;
  return (
    '<article class="panel live-offer" data-live-card="' +
    o.index +
    '">' +
    '<div class="live-offer-top"><div><span class="badge ' +
    status +
    '">' +
    esc(statusName(o)) +
    "</span>" +
    "<h3>" +
    esc(o.hotel_name || "Название гостиницы неизвестно") +
    "</h3>" +
    '<div class="live-subtitle">' +
    esc(o.room_name || "Тип номера неизвестен") +
    "</div></div>" +
    livePriceInfo(o) +
    "</div>" +
    '<div class="live-meta">' +
    esc(o.check_in || "?") +
    " — " +
    esc(o.check_out || "?") +
    " · " +
    esc(
      typeof o.room_count === "number"
        ? pluralRooms(o.room_count)
        : (o.room_count ?? "?") + " номеров",
    ) +
    " · " +
    esc(partyText(o.occupancy || [])) +
    "</div>" +
    '<div class="live-facts">' +
    features
      .map(
        (f) =>
          "<div><span>" + esc(f[0]) + "</span><b>" + esc(f[1]) + "</b></div>",
      )
      .join("") +
    "</div>" +
    '<div class="live-reasons">' +
    (reasons.length
      ? reasons
          .slice(0, 3)
          .map((r) => "<span>" + esc(reasonName[r] || r) + "</span>")
          .join("")
      : "<span>Нужна актуальная проверка тарифа</span>") +
    "</div>" +
    hotelDetailsHtml(o) +
    '<div class="live-origin">' +
    esc(o.source || "Поставщик") +
    " · " +
    (o.source_kind === "SANDBOX" ? "Тестовая среда · " : "") +
    "получено " +
    esc(window.traveltechObservedAt?.(o.observed_at) || "Время не указано") +
    "</div>" +
    '<details class="live-details"><summary>Условия отмены, курс и доказательства</summary>' +
    "<p><b>Отмена:</b> " +
    esc(refundable) +
    "</p>" +
    "<p><b>Основания:</b> " +
    esc(
      reasons.map((r) => reasonName[r] || r).join("; ") ||
        "Нет известных нарушений, наличие всё равно не гарантировано",
    ) +
    "</p>" +
    "<p><b>Проверенные поля:</b> " +
    esc(
      Object.entries(supplier)
        .filter(
          ([k, v]) => v && typeof v === "object" && v.field && "value" in v,
        )
        .map(([k, v]) => k + " = " + v.value + " (" + v.field + ")")
        .join("; ") || "Дополнительных свойств не найдено",
    ) +
    ". Код питания не является гарантией завтрака.</p>" +
    "<p><b>Идентификаторы поставщика:</b> hotel " +
    esc(o.hotel_id) +
    "; room " +
    esc(o.room_id) +
    "; rate " +
    esc(o.rate_code) +
    "</p>" +
    "<p><b>Состав цены:</b> " +
    esc(o.price_basis || "Не указан") +
    "; налоги " +
    (o.taxes_known === true ? "известны" : "не установлены") +
    ".</p>" +
    (converted.approximate
      ? "<p><b>Курс:</b> " +
        esc(converted.source || "неизвестный источник") +
        "; обновлён " +
        esc(converted.updated || "дата неизвестна") +
        ". Пересчёт приблизительный.</p>"
      : "") +
    "</details>" +
    '<div class="live-offer-actions"><label><input type="checkbox" data-live-compare="' +
    o.index +
    '" ' +
    (liveSelected.has(o.index) ? "checked" : "") +
    "> Сравнить</label>" +
    '<button type="button" class="quiet" data-availability="' +
    o.index +
    '" ' +
    (checkDisabled || o.source !== "TOURMIND_LIVE_RATE" ? "disabled " : "") +
    ">Проверить наличие · только чтение</button>" +
    '<button type="button" class="quiet" data-select-offer="' +
    o.index +
    '" ' +
    (checkDisabled ||
    o.status === "REJECT" ||
    o.recommendable === false ||
    o.quote_change?.requires_user_reacceptance
      ? "disabled "
      : "") +
    ">" +
    (saved
      ? "\u0412\u0430\u0440\u0438\u0430\u043d\u0442 \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d"
      : "\u0417\u0430\u043f\u043e\u043c\u043d\u0438\u0442\u044c \u0432\u0430\u0440\u0438\u0430\u043d\u0442 \u00b7 \u043d\u0435 \u0431\u0440\u043e\u043d\u044c") +
    "</button></div>" +
    "</article>"
  );
}
function liveRender() {
  const filter = document.getElementById("live-filter").value,
    sort = document.getElementById("live-sort").value;
  let offers = (liveResult?.offers || []).map((o, index) => ({ ...o, index }));
  if (filter === "eligible")
    offers = offers.filter((o) => o.status !== "REJECT");
  if (filter === "reject") offers = offers.filter((o) => o.status === "REJECT");
  if (sort !== "verified" && typeof window.traveltechSortOffers === "function")
    offers = window.traveltechSortOffers(offers, sort);
  else if (sort === "price") offers.sort(liveSortKey);
  const visibleIds = new Set(offers.slice(0, liveLimit).map((o) => o.index));
  for (const id of liveSelected)
    if (!visibleIds.has(id)) liveSelected.delete(id);
  const count = Math.min(liveLimit, offers.length);
  const summary =
    '<div class="live-count"><b>Показано ' +
    count +
    " из " +
    offers.length +
    " тарифов</b><small>Стоимость за все ночи и номера, в валюте поставщика. Пересчёт приблизительный; итоговая цена не подтверждена.</small></div>";
  document.getElementById("live-cards").innerHTML =
    summary +
    (offers.length
      ? offers.slice(0, liveLimit).map(liveOfferCard).join("")
      : "<p>Тарифов для выбранного фильтра нет. Пустая выдача и ошибка поставщика различаются в сообщении выше.</p>");
  document.querySelectorAll("[data-live-compare]").forEach(
    (input) =>
      (input.onchange = () => {
        const i = Number(input.dataset.liveCompare);
        input.checked ? liveSelected.add(i) : liveSelected.delete(i);
        liveCompare();
      }),
  );
  document.querySelectorAll("[data-availability]").forEach(
    (button) =>
      (button.onclick = () =>
        dataAction(async () => {
          if (liveResult?.__agentPreview)
            throw new Error(
              "Сначала подтвердите условия и выполните отдельный поиск",
            );
          if (!state || state.revision !== liveResult.revision)
            throw new Error("Условия изменились: обновите поиск");
          if (!liveResult.source_snapshot_id)
            throw new Error("MISSING_SOURCE_SNAPSHOT_ID");
          if (liveResult.snapshot_stale)
            throw new Error("STALE_SOURCE_SNAPSHOT_RESEARCH_AGAIN");
          const result = await api("/api/data/availability", {
            offer_index: Number(button.dataset.availability),
            sid: state.session_id,
            revision: state.revision,
            source_snapshot_id: liveResult.source_snapshot_id,
          });
          document.getElementById("data-details").textContent = JSON.stringify(
            result,
            null,
            2,
          );
          const refreshed = await api(
            "/api/session?id=" + encodeURIComponent(state.session_id),
          );
          render(refreshed);
          showDataSummary(result);
          dataFeedback(
            "Ответ read-only получен. Цена и наличие могут измениться, бронирование не выполнено.",
          );
        })),
  );
  document.querySelectorAll("[data-select-offer]").forEach(
    (button) =>
      (button.onclick = () =>
        dataAction(async () => {
          if (liveResult?.__agentPreview || !state)
            throw new Error("SAVE_ONLY_CONFIRMED_TRIP_SEARCH");
          if (
            liveResult.session_id !== state.session_id ||
            liveResult.revision !== state.revision
          )
            throw new Error("STALE_SELECTION_CONTEXT");
          if (!liveResult.source_snapshot_id || liveResult.snapshot_stale)
            throw new Error("STALE_SOURCE_SNAPSHOT_RESEARCH_AGAIN");
          const saved = await api("/api/data/select", {
            sid: state.session_id,
            revision: state.revision,
            source_snapshot_id: liveResult.source_snapshot_id,
            offer_index: Number(button.dataset.selectOffer),
          });
          liveSelection = saved.selected_offer;
          liveRender();
          dataFeedback(
            "\u0412\u0430\u0440\u0438\u0430\u043d\u0442 \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d \u0434\u043b\u044f \u044d\u0442\u043e\u0439 \u0432\u0435\u0440\u0441\u0438\u0438 \u043f\u043e\u0435\u0437\u0434\u043a\u0438. \u042d\u0442\u043e \u043d\u0435 \u0431\u0440\u043e\u043d\u044c \u0438 \u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d\u043d\u0430\u044f \u043e\u043a\u043e\u043d\u0447\u0430\u0442\u0435\u043b\u044c\u043d\u0430\u044f \u0446\u0435\u043d\u0430.",
          );
        })),
  );
  if (offers.length > liveLimit) {
    const more = document.createElement("button");
    more.type = "button";
    more.className = "quiet";
    more.textContent = "Показать ещё 20 тарифов";
    more.onclick = () => {
      liveLimit += 20;
      liveRender();
    };
    document.getElementById("live-cards").append(more);
  }
  liveCompare();
}
function liveCompare() {
  const items = [...liveSelected]
      .map((i) => liveResult?.offers?.[i])
      .filter(Boolean),
    box = document.getElementById("live-comparison");
  box.hidden = items.length < 2;
  if (items.length < 2) return;
  const rows = [
    ["Стоимость поставщика", (o) => quoteMoney(o.total, o.currency)],
    [
      "Примерно в валюте бюджета",
      (o) => {
        const q = fxQuote(o);
        return q.approximate
          ? "≈ " + quoteMoney(q.amount, q.currency)
          : q.comparable
            ? "В исходной валюте"
            : "Нет подтверждённого курса";
      },
    ],
    ["Тип номера", (o) => o.room_name || "Неизвестно"],
    ["Статус", statusName],
    [
      "Звёзды по поставщику",
      (o) =>
        o.supplier_facts?.star_rating?.field === "hotel.star_rating"
          ? String(o.supplier_facts.star_rating.value)
          : "Нет подтверждённого значения",
    ],
    [
      "Расстояние до точки поиска",
      (o) =>
        o.supplier_facts?.distance_to_search_anchor_km?.field ===
        "hotel.distance_km"
          ? String(o.supplier_facts.distance_to_search_anchor_km.value) + " км"
          : "Неизвестно",
    ],
    [
      "Завтрак",
      (o) =>
        o.breakfast === true
          ? "Включён"
          : o.breakfast === false
            ? "Не включён"
            : "Неизвестно",
    ],
    ["Налоги", (o) => (o.taxes_known === true ? "Подтверждены" : "Неизвестно")],
    [
      "Наличие",
      (o) =>
        o.availability_verified === true ? "Снимок проверки" : "Не проверено",
    ],
    [
      "Неизвестно или нарушения",
      (o) => (o.reasons || []).map((r) => reasonName[r] || r).join("; "),
    ],
    [
      "Момент получения",
      (o) => window.traveltechObservedAt?.(o.observed_at) || "Время не указано",
    ],
  ];
  box.innerHTML =
    "<table><thead><tr><th>Критерий</th>" +
    items.map((o) => "<th>" + esc(o.hotel_name) + "</th>").join("") +
    "</tr></thead><tbody>" +
    rows
      .map(
        ([label, fn]) =>
          "<tr><th>" +
          esc(label) +
          "</th>" +
          items.map((o) => "<td>" + esc(fn(o)) + "</td>").join("") +
          "</tr>",
      )
      .join("") +
    "</tbody></table>";
}
window.addEventListener("DOMContentLoaded", async () => {
  try {
    const cfg = await window.fullConfigPromise;
    document.getElementById("route-mode").textContent = window.fullReal
      ? "Реальные вызовы: только существующие бесплатные кредиты. Разбор условий — локальный, инструменты выбирает модель."
      : "Офлайн: полный интерфейс, локальный агент, без внешних вызовов.";
    window.traveltechMvpDeterministic = cfg.mvp_deterministic === true || location.hostname === "wing-updates-point-nvidia.trycloudflare.com";
    if (window.fullReal) {
      document.getElementById("agent-mode").checked = false;
      document.getElementById("skill-source").value = "live";
      document.querySelector("label:has(#agent-mode)").lastChild.textContent =
        " AI-помощник · изменения только после подтверждения";
      document.querySelector(".offline").textContent =
        "Локальное приложение · подключённый поиск";
      document.querySelector(".privacy-note").textContent =
        "При включении AI текст и условия отправляются выбранному провайдеру. Не вводите личные данные. Бронирование и оплата недоступны.";
      document.getElementById("data-mode").textContent =
        "Поиск у источника выполняется по вашему действию. При ошибке результат остаётся неизвестным.";
    } else {
      document
        .querySelectorAll(".current-data button")
        .forEach((b) => (b.disabled = true));
      document.getElementById("provider-route").disabled = true;
    }
    if (window.traveltechMvpDeterministic) {
      const mode = document.getElementById("agent-mode");
      mode.checked = false;
      mode.disabled = true;
      document.querySelector("label:has(#agent-mode)").lastChild.textContent =
        " MVP: локальный разбор без ИИ; гостиницы LiteAPI после подтверждения";
      document.getElementById("route-mode").textContent =
        "Тест MVP: ввод по-русски → подтверждение → поиск LiteAPI sandbox. Модель отключена.";
      document.getElementById("provider-route").disabled = true;
    } else {
      document.getElementById("agent-mode").disabled = false;
    }
    // A completed FAILED agent request from an older version must not
    // permanently lock the new MVP. Verify its durable receipt, never replay.
    if (
      window.traveltechMvpDeterministic &&
      sessionStorage.getItem("traveltech.full.pending")
    ) {
      try {
        await window.reconcileTravelTech();
      } catch (_) {
        // The pending marker remains; tracked operations stay protected.
      }
    }
    document.getElementById("resolve-location").onclick = () =>
      dataAction(async () => {
        liveLocation = await api("/api/data/location", {
          keyword: document.getElementById("location-query").value,
        });
        document.getElementById("location-choice").innerHTML =
          '<option value="">Выберите подходящий регион</option>' +
          liveLocation.regions
            .map(
              (r, i) =>
                `<option value="${i}">${esc(r.full_name || r.name)} · ${esc(r.region_type)} · ${esc(r.country)}</option>`,
            )
            .join("");
        document.getElementById("location-evidence").textContent =
          liveLocation.source +
          " · " +
          (window.traveltechObservedAt?.(liveLocation.observed_at) ||
            "Время не указано") +
          (liveLocation.place
            ? " · Точная точка: " +
              liveLocation.place.name +
              " / " +
              liveLocation.place.formatted_address
            : "");
        dataFeedback(
          "Локация получена. Проверьте название и страну, затем выберите регион или точную точку.",
        );
      });
    document.getElementById("resolve-geo").onclick = () =>
      dataAction(async () => {
        const result = await api("/api/data/geo", {
          keyword: document.getElementById("location-query").value,
        });
        document.getElementById("data-details").textContent = JSON.stringify(
          result,
          null,
          2,
        );
        showDataSummary(result);
        dataFeedback(
          "Места Photon / OpenStreetMap: только геоданные, без цен и наличия гостиниц.",
        );
      });
    document.getElementById("live-search").onclick = () =>
      dataAction(async () => {
        if (!state) throw new Error("Сначала подтвердите условия поездки");
        const location =
          window.traveltechPrimarySupplier === "LITEAPI"
            ? null
            : chosenLocation();
        const place =
          liveLocation?.regions?.[
            Number(document.getElementById("location-choice").value)
          ];
        if (location?.mode === "region" && !place)
          throw new Error("Выберите регион");
        const liveSid = state.session_id,
          liveRevision = state.revision,
          searchSerial = ++traveltechLiveViewSerial;
        liveResult = null;
        liveSelected.clear();
        liveLimit = 20;
        window.traveltechMvpSupplierView = {
          sid: liveSid, revision: liveRevision, pending: true,
        };
        window.traveltechRenderMvpSupplierResults?.();
        liveRender();
        let fresh;
        try {
          fresh = await api("/api/data/search", {
            sid: liveSid,
            revision: liveRevision,
            location,
          });
        } catch (error) {
          window.traveltechMvpSupplierView = {
            sid: liveSid, revision: liveRevision, error: true,
          };
          window.traveltechRenderMvpSupplierResults?.();
          throw error;
        }
        if (
          searchSerial !== traveltechLiveViewSerial ||
          state?.session_id !== liveSid ||
          state?.revision !== liveRevision
        )
          return;
        liveResult = fresh;
        if (window.traveltechMvpDeterministic) {
          document.getElementById("live-sort").value =
            document.getElementById("primary-sort")?.value || "price";
        }
        window.traveltechMvpSupplierView = {
          sid: liveSid, revision: liveRevision, result: fresh,
        };
        window.traveltechRenderMvpSupplierResults?.();
        liveRender();
        const savedTrip = await api(
          "/api/session?id=" + encodeURIComponent(liveSid),
        );
        if (
          searchSerial !== traveltechLiveViewSerial ||
          state?.session_id !== liveSid ||
          state?.revision !== liveRevision
        )
          return;
        render(savedTrip);
        document.getElementById("data-details").textContent = JSON.stringify(
          {
            status: liveResult.status,
            request: liveResult.request,
            location: liveResult.location,
            failures: liveResult.failures,
            fx: liveResult.fx,
          },
          null,
          2,
        );
        dataFeedback(
          `${liveResult.supplier || liveResult.source}: ${liveResult.status}; тарифов ${liveResult.offers.length}.${liveResult.fallback_used ? " Использован резерв TourMind: " + liveResult.fallback_reason + "." : ""}${liveResult.source_kind === "SANDBOX" ? " ТЕСТОВЫЕ данные LiteAPI; не подтверждают реальные цены и наличие." : ""} Наличие не проверено.`,
        );
      });
    document.getElementById("live-weather").onclick = () =>
      dataAction(async () => {
        if (!state) throw new Error("Подтвердите поездку");
        const location = chosenLocation(),
          region =
            liveLocation.regions[
              Number(document.getElementById("location-choice").value)
            ],
          point = location.mode === "nearby" ? location : region;
        const result = await api("/api/data/weather", {
          lat: point.latitude,
          lon: point.longitude,
          checkin: state.request.checkin,
          checkout: state.request.checkout,
        });
        document.getElementById("data-details").textContent = JSON.stringify(
          result,
          null,
          2,
        );
        showDataSummary(result);
        dataFeedback(
          result.forecast_available
            ? "Прогноз получен: Open-Meteo.com / CC BY 4.0; дата получения и даты прогноза ниже."
            : "Даты поездки вне горизонта прогноза 16 дней. Погодные значения не подставлены.",
        );
      });
    document.getElementById("live-filter").onchange = liveRender;
    document.getElementById("live-sort").onchange = () => {
      const primary = document.getElementById("primary-sort");
      const mode = document.getElementById("live-sort").value;
      if (primary && [...primary.options].some((option) => option.value === mode)) {
        primary.value = mode;
        window.traveltechRenderMvpSupplierResults?.();
      }
      liveRender();
    };
  } catch (e) {
    dataFeedback(
      "Не удалось загрузить настройки подключения. Условия поездки сохранены.",
      true,
    );
  }
});

window.addEventListener("DOMContentLoaded", async () => {
  try {
    const cfg = await window.fullConfigPromise;
    const route = document.getElementById("provider-route"),
      engine = document.getElementById("agent-engine");
    const unavailable = route.querySelector('option[value="Pollinations"]');
    if (unavailable) unavailable.remove();
    const original = new Set([...route.options].map((o) => o.value));
    for (const [id, p] of Object.entries(cfg.skill_routes || {})) {
      if (id === "Pollinations") continue;
      if (!original.has(id)) {
        const o = document.createElement("option");
        o.value = id;
        o.textContent = p.provider + " · " + p.model + " · навыки";
        o.dataset.skillsOnly = "1";
        route.append(o);
      }
    }
    for (const o of route.options) {
      const health = cfg.model_route_status?.[o.value];
      if (health && (health.enabled === false || health.blocked_reason)) {
        o.dataset.policyDisabled = "1";
        o.title =
          "Маршрут временно недоступен: требуется проверка доступа или исхода запроса.";
        o.textContent += " · недоступен";
      }
    }
    function update() {
      for (const o of route.options)
        o.disabled =
          Boolean(o.dataset.policyDisabled) ||
          (Boolean(o.dataset.skillsOnly) && engine.value !== "skills");
      if (
        route.selectedOptions[0]?.disabled ||
        (engine.value !== "skills" && route.value === "Auto")
      )
        route.value = "IISHKO";
      document.getElementById("skill-source").disabled =
        !window.fullReal || engine.value !== "skills";
      document.getElementById("jev-advice").disabled =
        !window.fullReal || engine.value !== "skills";
      document.getElementById("route-mode").textContent = window.fullReal
        ? engine.value === "skills"
          ? "Навыки: модель разбирает запрос, система проверяет условия и готовит предпросмотр. Источник выбирается отдельно."
          : "Прежний агент: локальный разбор и фиксированный цикл инструментов."
        : "Офлайн: система навыков с локальными правилами, без вызовов модели.";
    }
    engine.onchange = update;
    if (window.fullReal && engine.value === "skills") route.value = "Auto";
    update();
  } catch (e) {
    dataFeedback(
      window.traveltechErrorText?.(e) || "Не удалось проверить подключение.",
      true,
    );
  }
});

/* Skill agent read-only supplier projection. Does not grant a booking or availability capability. */
const baseLiveRender = liveRender;
liveRender = function () {
  baseLiveRender();
  if (liveResult?.__agentPreview) {
    document.querySelectorAll("[data-availability]").forEach((button) => {
      button.disabled = true;
      button.title =
        "Предварительный результат: сначала подтвердите условия поездки, затем выполните отдельный поиск";
    });
  }
};
window.addEventListener("traveltech-agent-live-update", (event) => {
  const value = event.detail?.result;
  if (!value) {
    if (liveResult) {
      liveResult = null;
      liveSelected.clear();
      liveSelection = null;
      liveLimit = 20;
      liveRender();
      dataFeedback(
        "Предварительная выдача агента сброшена после изменения состояния. Для проверки наличия выполните новый поиск.",
      );
    }
    return;
  }
  if (
    typeof value !== "object" ||
    !Array.isArray(value.offers) ||
    typeof value.request?.currency !== "string"
  ) {
    dataFeedback(
      "Ответ агента о тарифах не прошёл проверку структуры и не отображён.",
      true,
    );
    return;
  }
  liveResult = { ...value, __agentPreview: event.detail?.preview !== false };
  liveSelection = event.detail?.selected_offer || null;
  liveSelected.clear();
  liveLimit = 20;
  liveRender();
  document.getElementById("data-details").textContent = JSON.stringify(
    {
      status: value.status,
      observed_at: value.observed_at,
      source: value.source,
      search_scope: value.search_scope,
      location_coverage: value.location_coverage,
      candidate_count: value.candidate_count,
      offer_count: value.offers.length,
      availability_verified: false,
      booking_performed: false,
    },
    null,
    2,
  );
  dataFeedback(
    liveResult.__agentPreview
      ? "Предварительная выдача: " +
          value.offers.length +
          " тарифов. Сначала подтвердите условия поездки."
      : "Сохранённая выдача: " +
          value.offers.length +
          " тарифов. Наличие и окончательная цена не подтверждены.",
  );
});

/* Defence in depth: disable premature supplier checks even if another UI action
   temporarily re-enables controls. Enforced before availability click handlers. */
document.addEventListener(
  "click",
  (event) => {
    const button = event.target?.closest?.("[data-availability]");
    if (button && liveResult?.__agentPreview) {
      event.preventDefault();
      event.stopImmediatePropagation();
      button.disabled = true;
      dataFeedback(
        "Это предварительные предложения агента. Подтвердите поездку и выполните отдельный поиск для проверки наличия.",
        true,
      );
    }
  },
  true,
);

/* Shared, read-only UI predicate used after global button-state changes. */
window.traveltechPreviewActive = () => Boolean(liveResult?.__agentPreview);

/* Keep the primary result summary consistent with the separate live supplier section. */
window.addEventListener("traveltech-agent-live-update", (event) => {
  const result = event.detail?.result;
  if (!result || !Array.isArray(result.offers)) return;
  // Canonical supplier cards are already rendered in the primary trip view.
  if (event.detail?.canonical) return;
  const n = result.offers.length;
  document.getElementById("result-count").textContent =
    n +
    " текущий тариф" +
    (n === 1
      ? ""
      : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)
        ? "а"
        : "ов");
  document.getElementById("result-summary").textContent = n
    ? "Предварительный поиск TourMind: " +
      n +
      " тарифов. Модель сравнивала ограниченную выборку. Наличие и окончательный счёт не подтверждены."
    : "TourMind не нашёл доступных тарифов для проверенной территории и дат. Это не отсутствие гостиниц во всём городе.";
  document.getElementById("source-note").textContent =
    "Текущий ответ TourMind · только чтение · данные получены для условий нового запроса. Наличие, налоги и полный счёт пока не подтверждены.";
  document.getElementById("cards").innerHTML =
    '<div class="empty-state"><h3>Актуальная предварительная выдача — ниже</h3><p>До подтверждения поездки цены и наличие нельзя считать окончательными.</p><a href="#current-data">Открыть текущие тарифы ↓</a></div>';
});

/* TravelTech offline-only source controls disclosure (isolated UX candidate) */
window.addEventListener("DOMContentLoaded", async () => {
  let cfg;
  try {
    cfg = await window.fullConfigPromise;
  } catch {
    cfg = null;
  }
  if (cfg?.mode === "REAL_CREDIT_ONLY") return; // REAL supplier tools must stay visible.
  const panel = document.querySelector(".current-data");
  // New UX already contains its own disclosure; avoid hiding the tools twice.
  if (
    !panel ||
    panel.closest("details.source-tools") ||
    panel.closest("details.optional-live-tools")
  )
    return;
  const wrapper = document.createElement("details");
  wrapper.className = "optional-live-tools";
  wrapper.id = "optional-live-tools";
  const summary = document.createElement("summary");
  summary.className = "optional-live-summary";
  summary.innerHTML =
    '<span><strong>Подключение актуальных данных</strong><small>Поставщики, погода и геопоиск</small></span><span class="offline-label">Недоступно офлайн</span>';
  wrapper.appendChild(summary);
  const helper = document.createElement("p");
  helper.className = "optional-live-helper";
  helper.textContent =
    "В этой демонстрации внешние запросы отключены. Здесь можно посмотреть будущие интеграции, но без подключения источников поиск не выполняется.";
  wrapper.appendChild(helper);
  panel.parentNode.insertBefore(wrapper, panel);
  wrapper.appendChild(panel);
});

/* TravelTech advanced model settings disclosure (isolated UX candidate) */
window.addEventListener("DOMContentLoaded", async () => {
  let cfg;
  try {
    cfg = await window.fullConfigPromise;
  } catch {
    cfg = null;
  }
  const controls = document.querySelector(".route-controls");
  // The product-first interface already provides its own accessible disclosure.
  // Wrap only the legacy flat control panel; never create nested closed menus.
  if (
    !controls ||
    controls.tagName === "DETAILS" ||
    controls.closest("details.advanced-agent-settings")
  )
    return;
  const details = document.createElement("details");
  details.className = "advanced-agent-settings";
  details.open = cfg?.mode === "REAL_CREDIT_ONLY"; // Actual server config, not stale default.
  const summary = document.createElement("summary");
  summary.className = "advanced-agent-summary";
  summary.textContent = "Настройки агента и моделей";
  controls.parentNode.insertBefore(details, controls);
  details.append(summary, controls);
});

// A saved search is historical evidence, not a current supplier guarantee.
// Rehydrate it only for the exact trip version that produced the observation.
let traveltechSnapshotRestoreSerial = 0;
window.addEventListener("traveltech-trip-rendered", async (event) => {
  const sid = event.detail?.sid,
    revision = event.detail?.revision;
  const query = document.getElementById("location-query");
  const city = state?.request?.city || "";
  if (query && query.value !== city) {
    query.value = city;
    liveLocation = null;
    document.getElementById("location-choice").innerHTML =
      '<option value="">Выберите регион после поиска города</option>';
    document.getElementById("location-evidence").textContent = "";
  }
  if (!sid || typeof revision !== "number") {
    ++traveltechSnapshotRestoreSerial;
    invalidateLiveView();
    return;
  }
  const serial = ++traveltechSnapshotRestoreSerial;
  if (
    liveResult &&
    !liveResult.__agentPreview &&
    (liveResult.session_id !== sid || liveResult.revision !== revision)
  ) {
    invalidateLiveView();
  }
  // A trip can render before the asynchronous config/nonce handshake completes.
  // Wait for it, then reject stale restore work if another trip rendered meanwhile.
  try {
    await window.fullConfigPromise;
  } catch (_) {
    return;
  }
  if (
    serial !== traveltechSnapshotRestoreSerial ||
    state?.session_id !== sid ||
    state?.revision !== revision
  )
    return;
  if (!window.fullReal) return;
  if (liveResult?.__agentPreview) return;
  if (liveResult?.session_id === sid && liveResult?.revision === revision)
    return;
  try {
    const savedSelection = await api(
      "/api/data/selection?sid=" +
        encodeURIComponent(sid) +
        "&revision=" +
        encodeURIComponent(revision),
    );
    if (
      serial === traveltechSnapshotRestoreSerial &&
      state?.session_id === sid &&
      state?.revision === revision
    ) {
      liveSelection = savedSelection.selected_offer || null;
    }
  } catch (error) {
    /* Old trip without a selection or a revoked identity. */
  }
  try {
    const saved = await api(
      "/api/data/snapshot?sid=" +
        encodeURIComponent(sid) +
        "&revision=" +
        encodeURIComponent(revision),
    );
    if (
      serial !== traveltechSnapshotRestoreSerial ||
      state?.session_id !== sid ||
      state?.revision !== revision ||
      liveResult?.__agentPreview
    )
      return;
    liveResult = saved;
    liveSelected.clear();
    liveLimit = 20;
    liveRender();
    dataFeedback(
      saved.snapshot_stale
        ? "\u0412\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0430 \u0438\u0441\u0442\u043e\u0440\u0438\u0447\u0435\u0441\u043a\u0430\u044f \u0432\u044b\u0434\u0430\u0447\u0430. \u0414\u0430\u043d\u043d\u044b\u0435 \u0443\u0441\u0442\u0430\u0440\u0435\u043b\u0438: \u043f\u0435\u0440\u0435\u0434 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u043e\u0439 \u043d\u0430\u043b\u0438\u0447\u0438\u044f \u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u0435 \u043d\u043e\u0432\u044b\u0439 \u043f\u043e\u0438\u0441\u043a."
        : "\u0412\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0430 \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d\u043d\u0430\u044f \u0432\u044b\u0434\u0430\u0447\u0430 \u043f\u043e\u0441\u0442\u0430\u0432\u0449\u0438\u043a\u0430. \u0426\u0435\u043d\u0430 \u0438 \u043d\u0430\u043b\u0438\u0447\u0438\u0435 \u043d\u0435 \u0433\u0430\u0440\u0430\u043d\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u044b.",
    );
  } catch (error) {
    // An absent snapshot is normal for newly created or offline-only trips.
  }
});

window.addEventListener("DOMContentLoaded", () => {
  for (const id of [
    "location-query",
    "location-choice",
    "nearby-mode",
    "nearby-radius",
  ]) {
    const input = document.getElementById(id);
    if (!input) continue;
    input.addEventListener("change", () => invalidateLiveView());
    if (id === "location-query")
      input.addEventListener("input", () => invalidateLiveView());
  }
});

// An observed price/term change never changes the agreed quote until this click.
const rc20OriginalDataSummary = showDataSummary;
showDataSummary = function (result) {
  rc20OriginalDataSummary(result);
  if (!result.requires_user_reacceptance) return;
  const box = document.getElementById("data-summary");
  const note = document.createElement("p");
  note.textContent =
    "Цена, валюта или условия отмены изменились. Прежняя стоимость сохранена до вашего согласия. Это не бронирование.";
  box.appendChild(note);
  const before = document.createElement("p");
  before.textContent =
    "Прежняя стоимость: " +
    quoteMoney(result.previous_quote?.total, result.previous_quote?.currency);
  box.appendChild(before);
  for (const rate of result.checked_rates || []) {
    if (!rate.rate_binding || !rate.identity_verified) continue;
    const terms = document.createElement("p");
    terms.textContent =
      "Новая стоимость: " +
      quoteMoney(rate.total, rate.currency) +
      ". Отмена: " +
      (rate.cancellation?.refundable === false
        ? "поставщик указал невозвратный тариф"
        : rate.cancellation?.refundable === true
          ? "поставщик допускает возврат; сроки и штрафы ниже"
          : "не подтверждена") +
      ". " +
      (rate.cancellation?.cancelPolicyInfos
        ? JSON.stringify(rate.cancellation.cancelPolicyInfos)
        : "Сроки и штрафы отмены не подтверждены.");
    box.appendChild(terms);
  }
  if (!result.can_accept_changed_quote) return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "primary";
  button.textContent = "Согласиться с новой ценой и условиями";
  button.onclick = () =>
    dataAction(async () => {
      if (
        !state ||
        state.session_id !== result.session_id ||
        state.revision !== result.revision
      )
        throw new Error("STALE_QUOTE_CONTEXT");
      const accepted = await api("/api/data/accept_quote", {
        sid: state.session_id,
        revision: state.revision,
        source_snapshot_id: result.source_snapshot_id,
        offer_index: result.offer_index,
        check_id: result.check_id,
      });
      const updated = await api(
        "/api/session?id=" + encodeURIComponent(state.session_id),
      );
      render(updated);
      button.disabled = true;
      dataFeedback(
        "Новая наблюдаемая цена согласована и сохранена. Наличие и окончательный счёт не гарантированы; бронирование не выполнено.",
      );
    });
  box.appendChild(button);
};

for (const [key, label] of Object.entries({
  min_stars: "звёздность",
  pool: "бассейн",
  parking: "парковка",
  air_conditioning: "кондиционер",
  quiet: "тишина",
  max_distance_km: "расстояние",
})) {
  reasonName["HARD_REQUIREMENT_UNKNOWN:" + key] =
    "Обязательное условие не подтверждено: " + label;
  reasonName["HARD_REQUIREMENT_FAILED:" + key] =
    "Обязательное условие не выполнено: " + label;
}

Object.assign(reasonName, {
  FINAL_PAYABLE_PRICE_UNVERIFIED: "Окончательный счёт не подтверждён",
  SANDBOX_NOT_LIVE_INVENTORY:
    "Тестовая среда: реальные цена и наличие не подтверждены",
  SUPPLIER_DATES_REQUEST_BOUND:
    "Даты привязаны к запросу, отдельное подтверждение поставщика отсутствует",
  SUPPLIER_OCCUPANCY_UNVERIFIED: "Состав гостей отдельно не подтверждён",
  KNOWN_TOTAL_OVER_BUDGET: "Цена с известными доплатами превышает бюджет",
});

Object.assign(reasonName, {
  PROPERTY_FEES_REQUIRE_REVIEW:
    "В отеле могут быть отдельные платежи: проверьте сумму и условия",
  SUPPLIER_FACT_CONFLICT:
    "Сведения поставщика об удобствах противоречат друг другу",
  EXCLUDED_FEES_UNPRICED: "Отдельные сборы не удалось включить в расчёт",
});

window.addEventListener("DOMContentLoaded", async () => {
  try {
    const cfg = await window.fullConfigPromise;
    if (!window.fullReal) return;
    const status = await api("/api/supplier/status");
    window.traveltechPrimarySupplier = status.active_supplier;
    const label = document.createElement("label");
    label.textContent = "Гражданство гостей (для условий тарифа): ";
    const input = document.createElement("select");
    input.id = "guest-nationality";
    const countryNames = new Intl.DisplayNames(["ru"], { type: "region" });
    input.append(new Option("Выберите гражданство", ""));
    const countryCodes = [
      "AC",
      "AD",
      "AE",
      "AF",
      "AG",
      "AI",
      "AL",
      "AM",
      "AO",
      "AQ",
      "AR",
      "AS",
      "AT",
      "AU",
      "AW",
      "AX",
      "AZ",
      "BA",
      "BB",
      "BD",
      "BE",
      "BF",
      "BG",
      "BH",
      "BI",
      "BJ",
      "BL",
      "BM",
      "BN",
      "BO",
      "BQ",
      "BR",
      "BS",
      "BT",
      "BV",
      "BW",
      "BY",
      "BZ",
      "CA",
      "CC",
      "CD",
      "CF",
      "CG",
      "CH",
      "CI",
      "CK",
      "CL",
      "CM",
      "CN",
      "CO",
      "CP",
      "CQ",
      "CR",
      "CU",
      "CV",
      "CW",
      "CX",
      "CY",
      "CZ",
      "DE",
      "DG",
      "DJ",
      "DK",
      "DM",
      "DO",
      "DZ",
      "EA",
      "EC",
      "EE",
      "EG",
      "EH",
      "ER",
      "ES",
      "ET",
      "EU",
      "EZ",
      "FI",
      "FJ",
      "FK",
      "FM",
      "FO",
      "FR",
      "GA",
      "GB",
      "GD",
      "GE",
      "GF",
      "GG",
      "GH",
      "GI",
      "GL",
      "GM",
      "GN",
      "GP",
      "GQ",
      "GR",
      "GS",
      "GT",
      "GU",
      "GW",
      "GY",
      "HK",
      "HM",
      "HN",
      "HR",
      "HT",
      "HU",
      "IC",
      "ID",
      "IE",
      "IL",
      "IM",
      "IN",
      "IO",
      "IQ",
      "IR",
      "IS",
      "IT",
      "JE",
      "JM",
      "JO",
      "JP",
      "KE",
      "KG",
      "KH",
      "KI",
      "KM",
      "KN",
      "KP",
      "KR",
      "KW",
      "KY",
      "KZ",
      "LA",
      "LB",
      "LC",
      "LI",
      "LK",
      "LR",
      "LS",
      "LT",
      "LU",
      "LV",
      "LY",
      "MA",
      "MC",
      "MD",
      "ME",
      "MF",
      "MG",
      "MH",
      "MK",
      "ML",
      "MM",
      "MN",
      "MO",
      "MP",
      "MQ",
      "MR",
      "MS",
      "MT",
      "MU",
      "MV",
      "MW",
      "MX",
      "MY",
      "MZ",
      "NA",
      "NC",
      "NE",
      "NF",
      "NG",
      "NI",
      "NL",
      "NO",
      "NP",
      "NR",
      "NU",
      "NZ",
      "OM",
      "PA",
      "PE",
      "PF",
      "PG",
      "PH",
      "PK",
      "PL",
      "PM",
      "PN",
      "PR",
      "PS",
      "PT",
      "PW",
      "PY",
      "QA",
      "QO",
      "RE",
      "RO",
      "RS",
      "RU",
      "RW",
      "SA",
      "SB",
      "SC",
      "SD",
      "SE",
      "SG",
      "SH",
      "SI",
      "SJ",
      "SK",
      "SL",
      "SM",
      "SN",
      "SO",
      "SR",
      "SS",
      "ST",
      "SV",
      "SX",
      "SY",
      "SZ",
      "TA",
      "TC",
      "TD",
      "TF",
      "TG",
      "TH",
      "TJ",
      "TK",
      "TL",
      "TM",
      "TN",
      "TO",
      "TR",
      "TT",
      "TV",
      "TW",
      "TZ",
      "UA",
      "UG",
      "UM",
      "UN",
      "US",
      "UY",
      "UZ",
      "VA",
      "VC",
      "VE",
      "VG",
      "VI",
      "VN",
      "VU",
      "WF",
      "WS",
      "XA",
      "XB",
      "XK",
      "YE",
      "YT",
      "ZA",
      "ZM",
      "ZW",
      "ZZ",
    ];
    countryCodes
      .map((code) => [code, countryNames.of(code)])
      .sort((a, b) => a[1].localeCompare(b[1], "ru"))
      .forEach(([code, name]) => input.append(new Option(name, code)));
    input.placeholder = "RU — Россия";
    input.setAttribute("aria-label", "Гражданство гостей");
    input.autocomplete = "off";
    input.value = sessionStorage.getItem("traveltech.guest-nationality") || "";
    input.addEventListener("change", () =>
      sessionStorage.setItem(
        "traveltech.guest-nationality",
        input.value.trim().toUpperCase(),
      ),
    );
    label.append(input);
    // Citizenship is a search condition, not an engineering setting.
    document.getElementById("query").before(label);
    const note = document.createElement("p");
    note.id = "supplier-mode-status";
    note.textContent =
      "Поиск: " +
      status.active_supplier +
      (status.automatic_supplier_fallback
        ? " → TourMind (Сочи, Дубай, Стамбул)."
        : "") +
      (status.mode === "SANDBOX_NOT_LIVE_INVENTORY"
        ? " LiteAPI: тестовый режим, реальные цены и наличие не подтверждены."
        : "");
    document.querySelector(".route-controls").append(note);
  } catch (e) {
    dataFeedback("Не удалось загрузить настройки поставщика.", true);
  }
});

// Keep the effective mode beside the composer, including deliberate local mode.
window.addEventListener("DOMContentLoaded", async () => {
  const banner = document.createElement("div");
  banner.id = "chat-mode-status";
  banner.setAttribute("role", "status");
  banner.className = "muted";
  const text = document.createElement("span");
  const enable = document.createElement("button");
  enable.type = "button";
  enable.textContent = "Включить ИИ для сложных запросов";
  banner.append(text, enable);
  document.getElementById("query").before(banner);
  const mode = document.getElementById("agent-mode");
  function update() {
    const online =
      window.fullReal && mode.checked && !window.traveltechMvpDeterministic;
    text.textContent = online
      ? "ИИ включён · предложения для поездки пока тестовые. "
      : "Демо-режим · изменения применяются после вашего подтверждения. ";
    enable.hidden =
      window.traveltechMvpDeterministic || !window.fullReal || mode.checked;
  }
  enable.addEventListener("click", () => {
    if (window.traveltechMvpDeterministic) return;
    mode.checked = true;
    document.getElementById("agent-engine").value = "skills";
    document.getElementById("skill-source").value = "live";
    document.getElementById("provider-route").value = "Auto";
    mode.dispatchEvent(new Event("change"));
    document.getElementById("agent-engine").dispatchEvent(new Event("change"));
    update();
  });
  mode.addEventListener("change", update);
  try {
    await window.fullConfigPromise;
    update();
  } catch (e) {
    text.textContent =
      "Подключение не подтверждено. Обновите страницу перед отправкой.";
    enable.hidden = true;
  }
});
