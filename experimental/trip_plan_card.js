/* TripPlan v0 presentation only: no HTTP, session changes or price calculation. */
(function (root) {
  "use strict";
  const object = (value) => value && typeof value === "object" && !Array.isArray(value)
    ? value : {};
  const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const text = (value) => typeof value === "string" && value.trim()
    ? escape(value) : "нет данных";
  const currency = (value) => typeof value === "string" && /^[A-Z]{3}$/.test(value);
  const amount = (value) => typeof value === "string" && /^\d+\.\d{2}$/.test(value)
    && Number.isSafeInteger(Number(value.replace(".", "")));
  const money = (value, unit) => {
    if (!amount(value)) return "Цена неизвестна";
    if (!currency(unit)) return "Валюта: нет данных";
    return escape(new Intl.NumberFormat("ru-RU", {
      style: "currency", currency: unit, maximumFractionDigits: 2,
      minimumFractionDigits: value.endsWith(".00") ? 0 : 2,
    }).format(Number(value)));
  };
  function date(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value))
      return "нет данных";
    const parsed = new Date(value + "T00:00:00Z");
    if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value)
      return "нет данных";
    return escape(value.split("-").reverse().join("."));
  }
  function time(value) {
    if (typeof value !== "string") return "нет данных";
    const match = value.match(/^(\d{4}-\d{2}-\d{2})T([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(Z|[+-](?:0\d|1[0-4]):[0-5]\d)$/);
    if (!match || date(match[1]) === "нет данных" || !Number.isFinite(Date.parse(value)))
      return "нет данных";
    // Keep the local time and offset supplied by the backend, not the browser timezone.
    return `${date(match[1])} · ${match[2]}:${match[3]} (UTC${match[4] === "Z" ? "+00:00" : match[4]})`;
  }
  function source(part) {
    return `<small class="trip-plan-source">Источник: <b>${text(part.source_mode)}</b> · ${text(part.provider)}</small>`;
  }
  const row = (label, value) => `<div><dt>${label}</dt><dd>${value}</dd></div>`;
  function component(key, number, title, detail, part, stale) {
    return `<section class="trip-plan-component" data-component="${key}">
      <div class="trip-plan-component-heading"><span class="trip-plan-step" aria-hidden="true">${number}</span>
        <h3>${title}</h3><strong class="trip-plan-price">${stale ? "Устарело · STALE" : money(part.amount, part.currency)}</strong></div>
      <div class="trip-plan-component-body">${detail}${source(part)}</div>
    </section>`;
  }
  function travellers(value) {
    const party = object(value);
    const adults = Number.isInteger(party.adults) && party.adults > 0
      ? `${party.adults} ${party.adults % 10 === 1 && party.adults % 100 !== 11 ? "взрослый" : "взрослых"}`
      : "Взрослые: нет данных";
    const ages = Array.isArray(party.children) && party.children.every(
      (age) => Number.isInteger(age) && age >= 0 && age < 18,
    ) ? party.children : null;
    const ageText = (age) => `${age} ${age === 1 ? "год" : age >= 2 && age <= 4 ? "года" : "лет"}`;
    return `${adults} · ${ages === null ? "Дети: нет данных" : ages.length
      ? `Дети: ${ages.map(ageText).join(", ")}` : "без детей"}`;
  }

  function budgetSummary(plan, hotel, totalKnown) {
    if (plan.budget == null) return "";
    const budget = object(plan.budget);
    const hotelOnly = budget.scope === "hotel_only";
    const scopeKnown = hotelOnly || budget.scope === "whole_trip";
    const label = hotelOnly ? "Бюджет на отель" : budget.scope === "whole_trip"
      ? "Бюджет всей поездки" : "Бюджет";
    const limitKnown = amount(budget.amount) && currency(budget.currency);
    const cost = hotelOnly ? hotel.amount : totalKnown ? plan.total_amount : null;
    const costCurrency = hotelOnly ? hotel.currency : plan.currency;
    let message = "Сравнение с бюджетом требует перепроверки";
    let overBudget = false;
    if (budget.amount === null) message = "В полученном плане бюджет не указан";
    else if (!scopeKnown) message = "Уточните, бюджет на всю поездку или только на отель";
    else if (limitKnown && amount(cost) && costCurrency === budget.currency) {
      // Compare supplied prices and the user limit in exact minor units; no FX guesses.
      const delta = BigInt(cost.replace(".", "")) - BigInt(budget.amount.replace(".", ""));
      overBudget = delta > 0n;
      if (overBudget) {
        const difference = `${delta / 100n}.${String(delta % 100n).padStart(2, "0")}`;
        message = `${hotelOnly ? "Стоимость отеля превышает" : "Известные расходы превышают"} бюджет на ${money(difference, budget.currency)}`;
      } else message = `${hotelOnly ? "Стоимость отеля" : "Известные расходы"} в пределах лимита`;
    }
    return `<section class="trip-plan-limit${overBudget ? " trip-plan-limit-over" : ""}" aria-label="Ограничение бюджета">
      <h3>${label}</h3>
      ${limitKnown ? `<p class="trip-plan-limit-amount">${money(budget.amount, budget.currency)}</p>` : ""}
      <p>${message}</p>
      <p class="trip-plan-muted">Неизвестные и невключённые расходы нужно проверить отдельно.</p>
    </section>`;
  }

  function renderTripPlanCard(input, options) {
    const stale = object(options).stale === true;
    const demo = object(options).demo === true;
    const plan = object(input);
    const hotel = object(plan.hotel), transport = object(plan.transport);
    const transfer = object(plan.transfer), insurance = object(plan.insurance);
    const parts = [hotel, transport, transfer];
    if (plan.insurance != null) parts.push(insurance);
    const requiredUnknown = [hotel, transport, transfer].some(
      (part) => !amount(part.amount) || !currency(part.currency),
    );
    const currencyConflict = parts.some((part) => amount(part.amount)
      && part.currency !== plan.currency);
    // Compare decimal-string amounts as exact integer cents, not floating-point RUB.
    const declaredCents = amount(plan.total_amount)
      ? BigInt(plan.total_amount.replace(".", "")) : null;
    const calculatedCents = parts.reduce((sum, part) => sum
      + (amount(part.amount) && part.currency === plan.currency
        ? BigInt(part.amount.replace(".", "")) : 0n), 0n);
    const totalMismatch = !requiredUnknown && !currencyConflict
      && declaredCents !== null && declaredCents !== calculatedCents;
    const totalKnown = !stale && !requiredUnknown && !currencyConflict
      && currency(plan.currency) && declaredCents !== null && !totalMismatch;
    const warnings = (Array.isArray(plan.warnings) ? plan.warnings : []).map((value) => {
      const warning = object(value);
      return `<li><b>${text(warning.code)}</b> · ${text(warning.component)}<br>${text(warning.message)}</li>`;
    });
    if (totalMismatch) warnings.push("<li>Сумма компонентов не совпадает с итогом. Нужна перепроверка.</li>");
    if (!totalKnown) warnings.push("<li>Стоимость требует перепроверки: нет полной суммы в одной валюте.</li>");
    const modes = [...new Set(parts.map((part) => part.source_mode).filter(
      (mode) => typeof mode === "string" && mode.trim(),
    ))];
    const route = `${text(plan.origin)} → ${text(plan.destination)}`;
    const reverse = `${text(plan.destination)} → ${text(plan.origin)}`;
    const version = Number.isSafeInteger(plan.revision) && plan.revision >= 0
      ? `Версия ${plan.revision}` : "Версия: нет данных";
    const flightTitle = transport.kind === "flight" ? "Перелёт туда и обратно" : "Транспорт";
    const transferTitle = transfer.kind === "airport_hotel" ? "Аэропорт → отель" : "Трансфер";
    return `<article class="trip-plan-card panel" aria-label="Полный маршрут поездки">
      <header class="trip-plan-header">
        <div><p class="eyebrow">${demo ? "УЧЕБНЫЙ ПРИМЕР · MOCK" : "ПОЛНЫЙ МАРШРУТ"}</p><h2>${route}</h2>
          <p class="trip-plan-dates">${date(plan.start_date)} — ${date(plan.end_date)}</p>
          <p class="trip-plan-party">${travellers(plan.travellers)}</p>
          <div class="trip-plan-mobile-total"><strong>${totalKnown ? money(plan.total_amount, plan.currency) : "Стоимость уточняется"}</strong><span>Сумма известных компонентов</span></div></div>
        <span class="trip-plan-mode">${modes.length ? modes.map(escape).join(" / ") : "Источник: нет данных"}${stale ? " · STALE" : ""}</span>
      </header>
      ${demo ? '<p class="trip-plan-demo-note" role="note">Фиксированный MOCK, не персональный расчёт. Отправление и бюджет вашей сессии не проверены. Цены ниже — учебные; маршрут не пересчитывается.</p>' : ""}
      <div class="trip-plan-layout"><div class="trip-plan-route">
        ${component("transport", "01", flightTitle, `<dl class="trip-plan-flights">
          ${row(route, `Вылет: ${time(transport.departure_at)}<br>Прилёт: ${time(transport.arrival_at)}`)}
          ${row(reverse, `Вылет: ${time(transport.return_departure_at)}<br>Прилёт: ${time(transport.return_arrival_at)}`)}
        </dl>`, transport, stale)}
        ${component("transfer", "02", transferTitle, `<p>Длительность: нет данных</p>
          ${transfer.kind === "airport_hotel" ? "<p class=\"trip-plan-muted\">Обратный трансфер не включён</p>" : "<p>Направление: нет данных</p>"}`, transfer, stale)}
        ${component("hotel", "03", text(hotel.name), `<p>Заезд: ${date(hotel.checkin)} · Выезд: ${date(hotel.checkout)}</p>
          <p>Время заселения: нет данных</p>`, hotel, stale)}
        ${plan.insurance != null ? component("insurance", "+", "Страховка", amount(insurance.amount)
          ? "" : "<p>Не входит в сумму известных компонентов</p>", insurance, stale) : ""}
      </div><aside class="trip-plan-budget" aria-label="Стоимость и ограничения">
        <p class="eyebrow">СТОИМОСТЬ ПОЕЗДКИ</p>
        <p class="trip-plan-total-label">Сумма известных компонентов</p>
        <p class="trip-plan-total">${totalKnown ? money(plan.total_amount, plan.currency) : "нет данных"}</p>
        <p class="trip-plan-total-note">${totalKnown
          ? "Это не окончательная цена поездки. Учитывайте неизвестные цены и невключённые услуги."
          : "Стоимость требует перепроверки"}</p>
        ${stale ? "<p class=\"trip-plan-total-note\">Сравнение с бюджетом требует перепроверки.</p>" : demo
          ? '<p class="trip-plan-total-note">Учебная сумма не сравнивается с бюджетом вашей сессии.</p>' : budgetSummary(plan, hotel, totalKnown)}
        <div class="trip-plan-warnings"><h3>Что учесть</h3>
          ${warnings.length ? `<ul>${warnings.join("")}</ul>` : `<p>${Array.isArray(plan.warnings)
            ? "Дополнительных предупреждений нет" : "Предупреждения: нет данных"}</p>`}
          <p>Время заселения и длительность трансфера неизвестны. Стыковка требует проверки.</p>
        </div>
      </aside></div>
      <footer class="trip-plan-footer"><span>Trip ID: ${text(plan.trip_id)}</span><span>${version}</span></footer>
    </article>`;
  }
  if (typeof module === "object" && module.exports) module.exports = { renderTripPlanCard };
  else root.traveltechTripPlanCard = Object.freeze({ renderTripPlanCard });
})(globalThis);
