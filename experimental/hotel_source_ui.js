/* Hotel search/rate provenance. Opt-in view only: no network and no cart.
   LIVE means a response was received, not that the price is verified or payable.
   observed_amount is the supplier figure. hotel.amount stays null and is not a trip total.
   No HTTP route is documented for this screen, so the file does not mount itself.
   See docs/qa/HOTEL_PROVENANCE_UI_20261026.md and docs/research/PILOT_ADAPTER_20261025.md.
*/
(function (root) {
  "use strict";
  const BACKEND_ENDPOINT = null;
  const DISCLAIMER = "Не итоговая цена. Не цена бронирования. В итог поездки не включено.";
  const MODES = ["LIVE", "TEST_FIXTURE", "CACHED"];
  const MODE_TEXT = {
    LIVE: "LIVE · ответ получен. Это не проверенная цена.",
    CACHED: "CACHED · сохранённая копия. Свежесть не гарантируется. Это не проверенная цена.",
    TEST_FIXTURE: "TEST_FIXTURE · фикстура. Не живой поиск. Это не проверенная цена.",
  };
  const STATUSES = ["off", "idle", "loading", "observation", "empty", "stale", "error", "unavailable", "unknown"];
  const REQUIRED_WARNINGS = [
    "PRICE_BASIS_UNKNOWN", "TAXES_UNKNOWN", "FEES_UNKNOWN", "EXPIRY_UNKNOWN",
    "OCCUPANCY_UNVERIFIED", "LICENSE_UNCONFIRMED", "NOT_BOOKABLE_QUOTE",
  ];
  const OPTIONAL_WARNINGS = [
    "PRICE_UNKNOWN", "CURRENCY_UNKNOWN", "CURRENCY_MISMATCH", "RATE_CONTEXT_UNVERIFIED",
    "NOT_LIVE", "SEARCH_RATE_DIFFER",
  ];
  const WARNING_TEXT = {
    PRICE_BASIS_UNKNOWN: "База цены неизвестна",
    TAXES_UNKNOWN: "Налоги неизвестны",
    FEES_UNKNOWN: "Сборы неизвестны",
    EXPIRY_UNKNOWN: "Срок действия неизвестен",
    OCCUPANCY_UNVERIFIED: "Размещение не подтверждено",
    LICENSE_UNCONFIRMED: "Лицензия на показ не подтверждена",
    NOT_BOOKABLE_QUOTE: "Не предложение бронирования",
    PRICE_UNKNOWN: "Сумма не наблюдалась",
    CURRENCY_UNKNOWN: "Валюта неизвестна",
    CURRENCY_MISMATCH: "Валюта не совпадает с валютой запроса. Пересчёта нет",
    RATE_CONTEXT_UNVERIFIED: "Контекст ставки не подтверждён",
    NOT_LIVE: "Это не живой ответ",
    SEARCH_RATE_DIFFER: "Сумма поиска и сумма ставки различаются. Ни одна не выбрана ценой",
  };
  const BLOCKED_ENVELOPE = [
    "cart", "order", "booking", "booking_url", "bookingUrl", "total", "total_amount",
    "payable", "payment", "offers", "ready", "bookable", "checkout_url",
  ];
  const IDENTITY = [
    "research_handle", "hotel_id", "hotelId", "offer_id", "offerId", "url", "booking_url",
    "bookingUrl", "id", "image", "images", "instructions", "rate_code", "room_id", "cart", "order",
  ];
  const MULTIPLIERS = ["nights", "nightly", "night_price", "stay_total", "grand_total", "price", "computed", "multiplied", "total", "amount_total", "payable_amount"];
  const UNKNOWN_EQUALS = [
    ["price_basis", "UNKNOWN"], ["tax_status", "UNKNOWN"], ["fee_status", "UNKNOWN"],
    ["expiry_status", "UNKNOWN"], ["freshness_status", "UNKNOWN"], ["cancellation", "UNKNOWN"],
    ["occupancy_status", "UNVERIFIED"], ["terms_status", "PENDING"],
    ["quote_status", "UNVERIFIED"], ["observation_type", "SEARCH_OBSERVATION"],
    ["trust", "untrusted_supplier_data"],
  ];
  const TAGS = new Set(["SECTION", "H2", "H3", "P", "DIV", "DL", "DT", "DD", "SPAN"]);

  const owns = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const clone = (value) => value == null ? value : JSON.parse(JSON.stringify(value));
  const safeToken = (value) => typeof value === "string" && /^[A-Z][A-Z0-9_]{0,63}$/.test(value) ? value : "";
  const asArray = (value) => Array.isArray(value) ? value : [];

  function isoDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const year = Number(value.slice(0, 4));
    const month = Number(value.slice(5, 7));
    const day = Number(value.slice(8, 10));
    const date = new Date(Date.UTC(year, month - 1, day));
    if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
    return value;
  }

  function timestamp(value) {
    if (typeof value !== "string" || value.length > 40) return null;
    const match = /^(\d{4}-\d{2}-\d{2})T([01]\d|2[0-3]):([0-5]\d):([0-5]\d)(\.\d{1,6})?(Z|[+-](?:0\d|1[0-4]):[0-5]\d)$/.exec(value);
    if (!match || !isoDate(match[1]) || !Number.isFinite(Date.parse(value))) return null;
    return value;
  }

  function plain(value, max) {
    if (typeof value !== "string") return null;
    const text = Array.from(value).filter((char) => {
      const code = char.codePointAt(0);
      return code >= 32 && code !== 127;
    }).join("").slice(0, max);
    return text;
  }

  function decimal(value) {
    if (typeof value !== "string" || value.length > 64 || !/^\d+(\.\d+)?$/.test(value)) return null;
    return value;
  }

  function headline(status, mode, code) {
    if (status === "off") return "Источник выключен. Поиск не выполняется.";
    if (status === "idle") return "Источник включён. Запрос ещё не выполнялся. Не итоговая цена.";
    if (status === "loading") return "Чтение источника… Наблюдаемая сумма ещё не показана. Не итоговая цена.";
    if (status === "unavailable") return "Источник недоступен. Конечная точка в документации не задана. Запрос не отправлен.";
    if (status === "error") return code
      ? `Ошибка источника: ${code}. Наблюдения не показаны. Не итоговая цена.`
      : "Ошибка источника. Наблюдения не показаны. Не итоговая цена.";
    if (status === "unknown") return code
      ? `Состояние неизвестно (${code}). Цена и бронирование не показаны.`
      : "Состояние неизвестно. Цена и бронирование не показаны.";
    if (status === "stale") return "STALE · Наблюдаемые суммы скрыты. Это не актуальная цена. Не итоговая цена.";
    const prefix = mode === "LIVE"
      ? "Pilot · LIVE: ответ получен. Это не проверенная цена."
      : mode === "CACHED"
        ? "Pilot · CACHED: сохранённое наблюдение. Свежесть не гарантируется. Это не проверенная цена."
        : mode === "TEST_FIXTURE"
          ? "Pilot · TEST_FIXTURE: фикстура проверки, не живой поиск. Это не проверенная цена."
          : "Режим источника неизвестен. Это не проверенная цена.";
    if (status === "empty") return `${prefix} Предложений нет. Ноль и итог не подставлены. Не итоговая цена.`;
    return `${prefix} Наблюдение поиска. Не итоговая цена.`;
  }

  function blank(status, code) {
    const token = safeToken(code);
    return {
      status, code: token || null, headline: headline(status, null, token), disclaimer: DISCLAIMER,
      sourceMode: null, observedAt: null, observations: [], cart: null, order: null,
      bookable: false, ready: false, payable: false, totalAmount: null,
    };
  }

  function field(id, label, value, source, observedAt, note) {
    return { id, label, value, note: note || "", source, observedAt };
  }

  function accept(row) {
    if (!object(row)) return { ok: false, code: "INPUT_UNKNOWN" };
    if (IDENTITY.some((key) => owns(row, key))) return { ok: false, code: "SUPPLIER_IDENTITY_REJECTED" };
    if (MULTIPLIERS.some((key) => owns(row, key))) return { ok: false, code: "PRICE_MULTIPLIER" };
    if (!MODES.includes(row.source_mode)) return { ok: false, code: "MODE_UNKNOWN" };
    if (row.source !== "Pilot" || row.provider !== "Pilot") return { ok: false, code: "SOURCE_UNCONFIRMED" };
    if (row.observation_kind !== "search" && row.observation_kind !== "rates")
      return { ok: false, code: "FIELD_NOT_UNKNOWN" };
    if (!owns(row, "amount") || row.amount !== null) return { ok: false, code: "AMOUNT_PRESENT" };
    if (row.eligible_for_verified_total !== false) return { ok: false, code: "TOTAL_FLAG_REJECTED" };
    if (row.availability !== "SEARCH_ONLY") return { ok: false, code: "AVAILABILITY_NOT_SEARCH" };
    if (row.quote_status !== "UNVERIFIED") return { ok: false, code: "QUOTE_NOT_UNVERIFIED" };
    if (row.expires_at !== null) return { ok: false, code: "FIELD_NOT_UNKNOWN" };
    for (const [key, expected] of UNKNOWN_EQUALS) {
      if (row[key] !== expected) return { ok: false, code: "FIELD_NOT_UNKNOWN" };
    }
    if (row.refundable !== true && row.refundable !== false && row.refundable !== "UNKNOWN")
      return { ok: false, code: "FIELD_NOT_UNKNOWN" };
    const amount = row.observed_amount === null ? null : decimal(row.observed_amount);
    if (row.observed_amount !== null && amount === null) return { ok: false, code: "PRICE_FORMAT" };
    const currency = row.currency === null ? null : (typeof row.currency === "string" && /^[A-Z]{3}$/.test(row.currency) ? row.currency : undefined);
    if (currency === undefined) return { ok: false, code: "CURRENCY_FORMAT" };
    if (!Array.isArray(row.warnings) || row.warnings.some((code) => typeof code !== "string" || !WARNING_TEXT[code]))
      return { ok: false, code: "WARNINGS_REQUIRED" };
    const warnings = [...new Set(row.warnings)];
    if (REQUIRED_WARNINGS.some((code) => !warnings.includes(code))) return { ok: false, code: "WARNINGS_REQUIRED" };
    const live = row.source_mode === "LIVE";
    if (live === warnings.includes("NOT_LIVE")) return { ok: false, code: "NOT_LIVE_MISMATCH" };
    if ((amount === null) !== warnings.includes("PRICE_UNKNOWN")) return { ok: false, code: "WARNINGS_REQUIRED" };
    if ((currency === null) !== warnings.includes("CURRENCY_UNKNOWN")) return { ok: false, code: "WARNINGS_REQUIRED" };
    if (currency === null && warnings.includes("CURRENCY_MISMATCH")) return { ok: false, code: "WARNINGS_REQUIRED" };
    if ((row.observation_kind === "rates") !== warnings.includes("RATE_CONTEXT_UNVERIFIED"))
      return { ok: false, code: "WARNINGS_REQUIRED" };
    if (row.observation_kind === "search" && warnings.includes("SEARCH_RATE_DIFFER"))
      return { ok: false, code: "WARNINGS_REQUIRED" };
    const name = plain(row.name, 300);
    const room = plain(row.room_name, 300);
    const board = plain(row.board, 300);
    if (name === null || room === null || board === null) return { ok: false, code: "FIELD_NOT_UNKNOWN" };
    const observedAt = timestamp(row.observed_at);
    const source = `Pilot · ${row.source_mode}`;
    const time = observedAt || "неизвестно";
    const kind = row.observation_kind === "rates" ? "наблюдение" : "поиск";
    const amountValue = amount === null
      ? "Сумма не наблюдалась. Ноль не подставлен."
      : currency ? `${amount} ${currency}` : `${amount} · валюта неизвестна`;
    const amountNote = amount === "0"
      ? "Наблюдаемый ноль не является итогом и не означает бесплатное проживание. Не к оплате. Сумма не умножается на ночи."
      : "Не к оплате. Не итоговая цена. Сумма не умножается на ночи.";
    const occupancy = occupancyText(row.requested_occupancy);
    const checkin = isoDate(row.checkin);
    const checkout = isoDate(row.checkout);
    const modeCopy = MODE_TEXT[row.source_mode];
    const refund = row.refundable === true
      ? "Поставщик передал признак возврата: да. Политика отмены не подтверждена."
      : row.refundable === false
        ? "Поставщик передал признак возврата: нет. Политика отмены не подтверждена."
        : "неизвестно";
    return {
      ok: true,
      item: {
        refused: false,
        title: name.trim() ? name : "название неизвестно",
        kind,
        sourceMode: row.source_mode,
        observedAt,
        warnings,
        fields: [
          field("mode", "Режим источника", modeCopy, source, time, ""),
          field("kind", "Вид записи", kind, source, time, kind === "поиск" ? "Это поиск, не ставка бронирования." : "Это наблюдение ставок, не ставка бронирования."),
          field("observed_amount", "Наблюдаемая сумма поставщика", amountValue, source, time, amountNote),
          field("currency", "Валюта", currency || "неизвестно", source, time, currency ? "Валюта наблюдения, не валюта итога поездки." : "Валюта неизвестна. Валюта запроса не подставлена."),
          field("price_basis", "База цены", "неизвестно", source, time, "Ночь или весь период не подтверждены."),
          field("tax", "Налоги", "неизвестно", source, time, ""),
          field("fee", "Сборы", "неизвестно", source, time, ""),
          field("expiry", "Срок действия", "неизвестно", source, time, ""),
          field("freshness", "Свежесть", "неизвестно", source, time, ""),
          field("checkin", "Заезд", checkin || "даты неизвестны", source, time, "Дата запроса, не подтверждённый заезд."),
          field("checkout", "Выезд", checkout || "даты неизвестны", source, time, "Дата запроса, не подтверждённый выезд."),
          field("occupancy", "Размещение", occupancy, source, time, "Состав запроса поставщиком не подтверждён."),
          field("board", "Питание", board.trim() ? board : "неизвестно", source, time, ""),
          field("room", "Номер", room.trim() ? room : "неизвестно", source, time, ""),
          field("refund", "Возврат", refund, source, time, ""),
          field("license", "Лицензия и условия", "лицензия и условия показа не подтверждены", source, time, ""),
          field("warnings", "Предупреждения", warnings.map((code) => WARNING_TEXT[code]).join("; "), source, time, ""),
          field("component_amount", "Итоговая цена", "неизвестна", source, time, "Ноль не подставлен."),
          field("booking", "Бронирование", "недоступно", source, time, "В демо-корзину не добавляется."),
        ],
      },
    };
  }

  function occupancyText(value) {
    if (!object(value)) return "размещение неизвестно";
    const guests = value.guests;
    const rooms = value.rooms;
    const children = value.children_ages;
    if (!Number.isInteger(guests) || guests < 1 || guests > 8) return "размещение неизвестно";
    if (!Number.isInteger(rooms) || rooms < 1 || rooms > 4) return "размещение неизвестно";
    if (!Array.isArray(children) || children.length > 6
      || children.some((age) => !Number.isInteger(age) || age < 0 || age > 17)) return "размещение неизвестно";
    const childText = children.length ? children.join(", ") : "нет";
    return `Запрос: гости ${guests}, номера ${rooms}, дети: ${childText}. Поставщик это не подтвердил.`;
  }

  function refusal(code) {
    const token = safeToken(code) || "INPUT_UNKNOWN";
    return { refused: true, code: token, title: "Наблюдение скрыто", kind: "", sourceMode: null, observedAt: null, warnings: [], fields: [
      field("refusal", "Причина", token, "этот экран", "неизвестно", "Сумма и бронирование скрыты."),
    ] };
  }

  function hotelEnvelope(input) {
    if (!owns(input, "observation")) return null;
    if (owns(input, "observations") || !object(input.observation)) return { error: "INPUT_UNKNOWN" };
    if (!owns(input, "amount") || input.amount !== null) return { error: "AMOUNT_PRESENT" };
    const currency = owns(input, "currency") ? input.currency : null;
    if (!(currency === null || (typeof currency === "string" && /^[A-Z]{3}$/.test(currency))))
      return { error: "CURRENCY_FORMAT" };
    if (currency !== null && input.observation.currency !== currency) return { error: "CURRENCY_CONFLICT" };
    return {
      envelope: {
        status: "OK",
        observations: [input.observation],
        source: input.provider,
        source_mode: input.source_mode,
        observed_at: input.observation.observed_at,
      },
    };
  }

  function project(input, options) {
    const stale = Boolean(object(options) && options.stale);
    const view = projectInner(input);
    if (stale && (view.status === "observation" || view.status === "empty")) return conceal(view);
    return view;
  }

  function projectInner(input) {
    if (!object(input)) return blank("unknown", "INPUT_UNKNOWN");
    if (BLOCKED_ENVELOPE.some((key) => owns(input, key))) return blank("unknown", "CART_OR_TOTAL_REJECTED");
    const bridged = hotelEnvelope(input);
    if (bridged) return bridged.error ? blank("unknown", bridged.error) : projectInner(bridged.envelope);
    if (input.status === "NO_OFFERS") {
      if (asArray(input.observations).length) return blank("unknown", "STATUS_UNKNOWN");
      const mode = MODES.includes(input.source_mode) ? input.source_mode : null;
      if (input.source_mode != null && mode === null) return blank("unknown", "MODE_UNKNOWN");
      const view = blank("empty", null);
      view.sourceMode = mode;
      view.observedAt = timestamp(input.observed_at);
      view.headline = headline("empty", mode, "");
      return view;
    }
    if (input.status !== "OK") return blank("unknown", "STATUS_UNKNOWN");
    if (!Array.isArray(input.observations)) return blank("unknown", "INPUT_UNKNOWN");
    if (input.observations.length > 50) return blank("unknown", "COLLECTION_LIMIT");
    if (input.observations.length === 0) {
      const empty = blank("empty", null);
      const emptyMode = MODES.includes(input.source_mode) ? input.source_mode : null;
      if (input.source_mode != null && emptyMode === null) return blank("unknown", "MODE_UNKNOWN");
      empty.sourceMode = emptyMode;
      empty.observedAt = timestamp(input.observed_at);
      empty.headline = headline("empty", emptyMode, "");
      return empty;
    }
    const envelopeMode = input.source_mode == null ? null : input.source_mode;
    if (envelopeMode !== null && !MODES.includes(envelopeMode)) return blank("unknown", "MODE_UNKNOWN");
    if (input.source != null && input.source !== "Pilot") return blank("unknown", "SOURCE_UNCONFIRMED");
    const items = [];
    for (const row of input.observations) {
      const result = accept(row);
      if (!result.ok) items.push(refusal(result.code));
      else if (envelopeMode && result.item.sourceMode !== envelopeMode) return blank("unknown", "MODE_MISMATCH");
      else items.push(result.item);
    }
    if (!items.some((item) => !item.refused)) {
      return blank("unknown", items[0] ? items[0].code : "INPUT_UNKNOWN");
    }
    const modes = [...new Set(items.filter((item) => !item.refused).map((item) => item.sourceMode))];
    const stamps = [...new Set(items.filter((item) => !item.refused).map((item) => item.observedAt).filter(Boolean))];
    const view = blank("observation", null);
    view.sourceMode = modes.length === 1 ? modes[0] : null;
    view.observedAt = stamps.length === 1 ? stamps[0] : null;
    view.observations = items;
    view.headline = headline("observation", view.sourceMode, "");
    return view;
  }

  function conceal(view) {
    const next = clone(view) || blank("stale", null);
    next.status = "stale";
    next.headline = headline("stale", null, "");
    next.totalAmount = null;
    next.payable = false;
    next.bookable = false;
    next.ready = false;
    next.cart = null;
    next.order = null;
    for (const item of asArray(next.observations)) {
      for (const entry of asArray(item.fields)) {
        if (entry.id === "observed_amount") {
          entry.value = "Скрыто · STALE";
          entry.note = "Сумма скрыта, потому что наблюдение устарело. Ноль не подставлен.";
        }
      }
    }
    return next;
  }

  function shownAmount(value, stale) {
    if (stale) return "Скрыто · STALE";
    if (value === "Сумма не наблюдалась. Ноль не подставлен.") return value;
    if (typeof value === "string" && /^(\d+(?:\.\d+)?)( [A-Z]{3}| · валюта неизвестна)?$/.test(value)) return value;
    return "неизвестно";
  }

  const FIELD_LABELS = {
    mode: "Режим источника", kind: "Вид записи", observed_amount: "Наблюдаемая сумма поставщика",
    currency: "Валюта", price_basis: "База цены", tax: "Налоги", fee: "Сборы", expiry: "Срок действия",
    freshness: "Свежесть", checkin: "Заезд", checkout: "Выезд", occupancy: "Размещение", board: "Питание",
    room: "Номер", refund: "Возврат", license: "Лицензия и условия", warnings: "Предупреждения",
    component_amount: "Итоговая цена", booking: "Бронирование", refusal: "Причина",
  };

  function shownField(entry, stale) {
    const id = typeof entry.id === "string" ? entry.id : "";
    if (!owns(FIELD_LABELS, id)) return null;
    const source = /^(Pilot · (LIVE|TEST_FIXTURE|CACHED)|этот экран)$/.test(entry.source)
      ? entry.source : "источник неизвестен";
    const observedAt = timestamp(entry.observedAt) || "неизвестно";
    let value = "неизвестно";
    let note = "";
    if (id === "observed_amount") {
      value = shownAmount(entry.value, stale);
      note = stale
        ? "Сумма скрыта, потому что наблюдение устарело. Ноль не подставлен."
        : /^0(?:\.0+)?(?: |·|$)/.test(value)
          ? "Наблюдаемый ноль не является итогом и не означает бесплатное проживание. Не к оплате. Сумма не умножается на ночи."
          : "Не к оплате. Не итоговая цена. Сумма не умножается на ночи.";
    } else if (id === "currency") {
      value = typeof entry.value === "string" && /^[A-Z]{3}$/.test(entry.value) ? entry.value : "неизвестно";
      note = value === "неизвестно" ? "Валюта неизвестна. Валюта запроса не подставлена." : "Валюта наблюдения, не валюта итога поездки.";
    } else if (id === "component_amount") { value = "неизвестна"; note = "Ноль не подставлен."; }
    else if (id === "booking") { value = "недоступно"; note = "В демо-корзину не добавляется."; }
    else if (id === "price_basis") { value = "неизвестно"; note = "Ночь или весь период не подтверждены."; }
    else if (id === "tax" || id === "fee" || id === "expiry" || id === "freshness") value = "неизвестно";
    else if (id === "license") value = "лицензия и условия показа не подтверждены";
    else if (id === "kind") {
      value = entry.value === "поиск" || entry.value === "наблюдение" ? entry.value : "неизвестно";
      note = value === "наблюдение" ? "Это наблюдение ставок, не ставка бронирования." : "Это поиск, не ставка бронирования.";
    }     else if (id === "mode") value = Object.values(MODE_TEXT).includes(entry.value) ? entry.value : "неизвестно";
    else if (id === "checkin" || id === "checkout") {
      value = isoDate(entry.value) || "даты неизвестны";
      note = id === "checkin" ? "Дата запроса, не подтверждённый заезд." : "Дата запроса, не подтверждённый выезд.";
    } else if (id === "refusal") { value = safeToken(entry.value) || "INPUT_UNKNOWN"; note = "Сумма и бронирование скрыты."; }
    else if (id === "warnings") {
      const parts = typeof entry.value === "string" ? entry.value.split("; ") : [];
      const known = new Set(Object.values(WARNING_TEXT));
      value = parts.length && parts.every((part) => known.has(part)) ? entry.value : "неизвестно";
    } else if (id === "refund") {
      value = entry.value === "неизвестно"
        || entry.value === "Поставщик передал признак возврата: да. Политика отмены не подтверждена."
        || entry.value === "Поставщик передал признак возврата: нет. Политика отмены не подтверждена."
        ? entry.value : "неизвестно";
    } else if (id === "occupancy") {
      value = entry.value === "размещение неизвестно"
        || (typeof entry.value === "string" && /^Запрос: гости [1-8], номера [1-4], дети: (нет|\d{1,2}(?:, \d{1,2})*)\. Поставщик это не подтвердил\.$/.test(entry.value))
        ? entry.value : "размещение неизвестно";
      note = "Состав запроса поставщиком не подтверждён.";
    } else if ((id === "board" || id === "room") && typeof entry.value === "string") value = entry.value.slice(0, 300);
    return { id, label: FIELD_LABELS[id], value, note, source, observedAt };
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (!TAGS.has(node.tagName)) throw Error("HOTEL_SOURCE_TAG");
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function render(host, view) {
    if (!host || typeof document === "undefined" || typeof document.createElement !== "function"
      || typeof host.replaceChildren !== "function") throw Error("HOTEL_SOURCE_HOST");
    const status = object(view) && STATUSES.includes(view.status) ? view.status : "unknown";
    const mode = object(view) && MODES.includes(view.sourceMode) ? view.sourceMode : null;
    const code = safeToken(object(view) && view.code);
    const section = element("section", "hotel-source");
    section.setAttribute("aria-label", "Наблюдение поиска отеля");
    section.setAttribute("data-status", status);
    section.setAttribute("data-bookable", "false");
    section.setAttribute("data-ready", "false");
    section.setAttribute("data-payable", "false");
    const title = element("h2", "hotel-source-title", "Наблюдение поиска отеля");
    const state = element("p", "hotel-source-status", headline(status, mode, code));
    state.setAttribute("role", "status");
    const disclaimer = element("p", "hotel-source-disclaimer", DISCLAIMER);
    section.append(title, state, disclaimer);
    if (status === "observation" || status === "stale") {
      const list = element("div", "hotel-source-list");
      for (const item of asArray(object(view) && view.observations).slice(0, 50)) {
        list.append(renderItem(item, status === "stale"));
      }
      section.append(list);
    }
    section.append(element("p", "hotel-source-cart", "В демо-корзину не добавляется."));
    host.replaceChildren(section);
    return section;
  }

  function renderItem(item, stale) {
    const wrap = element("div", "hotel-source-item");
    const refused = object(item) && item.refused === true;
    const title = element("h3", "hotel-source-name", refused
      ? "Наблюдение скрыто"
      : (object(item) && typeof item.title === "string" && item.title.trim() ? item.title.slice(0, 300) : "название неизвестно"));
    const fields = element("dl", "hotel-source-fields");
    const sourceRows = refused
      ? [field("refusal", "Причина", safeToken(item && item.code) || "INPUT_UNKNOWN", "этот экран", "неизвестно", "Сумма и бронирование скрыты.")]
      : asArray(object(item) && item.fields);
    for (const entry of sourceRows) {
      if (!object(entry)) continue;
      const shown = shownField(entry, stale);
      if (!shown) continue;
      const row = element("div", "hotel-source-field");
      row.setAttribute("data-field", shown.id);
      const label = element("dt", "", shown.label);
      const value = element("dd", "hotel-source-value", shown.value);
      const meta = element("span", "hotel-source-meta", `Источник: ${shown.source}. Время: ${shown.observedAt}.`);
      row.append(label, value, meta);
      if (shown.note) row.append(element("span", "hotel-source-note", shown.note));
      fields.append(row);
    }
    wrap.append(title, fields);
    return wrap;
  }

  function contextKey(value) {
    if (!object(value)) return "";
    return JSON.stringify({
      location: typeof value.location === "string" ? value.location.slice(0, 160) : null,
      check_in: typeof value.check_in === "string" ? value.check_in : null,
      check_out: typeof value.check_out === "string" ? value.check_out : null,
      guests: Number.isInteger(value.guests) ? value.guests : null,
      rooms: Number.isInteger(value.rooms) ? value.rooms : null,
      children_ages: Array.isArray(value.children_ages) ? value.children_ages : null,
      currency: typeof value.currency === "string" ? value.currency : null,
      guest_nationality: typeof value.guest_nationality === "string" ? value.guest_nationality : null,
    });
  }

  function createController(options) {
    const settings = object(options) ? options : {};
    const request = settings.request;
    const onChange = typeof settings.onChange === "function" ? settings.onChange : function () {};
    let host = settings.host || null;
    let enabled = false;
    let serial = 0;
    let trip = "";
    let view = blank("off", null);

    function seal(next) {
      const copy = clone(next) || blank("unknown", "INPUT_UNKNOWN");
      copy.cart = null;
      copy.order = null;
      copy.bookable = false;
      copy.ready = false;
      copy.payable = false;
      copy.totalAmount = null;
      copy.enabled = enabled;
      return copy;
    }
    function publish() {
      const snap = seal(view);
      if (host) render(host, snap);
      onChange(snap);
      return snap;
    }
    function setEnabled(on) {
      serial += 1;
      enabled = on === true;
      view = enabled ? blank("idle", null) : blank("off", null);
      return publish();
    }
    function updateContext(next) {
      const key = contextKey(next);
      if (key === trip) return seal(view);
      trip = key;
      serial += 1;
      if (view.status === "observation" || view.status === "empty" || view.status === "stale") view = conceal(view);
      else if (enabled && view.status === "loading") view = blank("idle", null);
      return publish();
    }
    async function load(query) {
      if (!enabled) return seal(view);
      if (typeof request !== "function") {
        serial += 1;
        view = blank("unavailable", "ENDPOINT_NOT_CONFIGURED");
        return publish();
      }
      const token = ++serial;
      const key = trip;
      view = blank("loading", null);
      publish();
      try {
        const result = await request(query);
        if (token !== serial || key !== trip) return seal(view);
        view = project(result);
      } catch (failure) {
        if (token !== serial || key !== trip) return seal(view);
        view = blank("error", safeToken(failure && failure.message) || "SOURCE_ERROR");
      }
      return publish();
    }
    function present(payload) {
      if (!enabled) return seal(view);
      serial += 1;
      view = project(payload);
      return publish();
    }
    function markStale() {
      serial += 1;
      if (view.status === "off") return seal(view);
      view = conceal(view.status === "observation" || view.status === "empty" || view.status === "stale"
        ? view : blank("stale", null));
      return publish();
    }
    function attach(next) {
      host = next || null;
      if (host) render(host, seal(view));
      return seal(view);
    }
    if (host) render(host, seal(view));
    return {
      setEnabled, updateContext, load, present, markStale, attach, render,
      snapshot: () => seal(view),
      exportForCart: () => null,
    };
  }

  const api = Object.freeze({
    BACKEND_ENDPOINT, createController, project, render, conceal,
    exportForCart: () => null,
  });
  if (typeof module === "object" && module.exports) module.exports = api;
  root.traveltechHotelSource = api;
})(globalThis);
