"use strict";
(async () => {
  const target = document.getElementById("trip-plan-preview");
  try {
    const response = await fetch("./trip_plan_v0.json");
    if (!response.ok) throw new Error("FIXTURE_UNAVAILABLE");
    target.innerHTML = traveltechTripPlanCard.renderTripPlanCard(await response.json());
  } catch (_) {
    // This preview needs an HTTP origin to read its adjacent JSON file.
    target.replaceChildren();
    const message = document.createElement("p");
    message.setAttribute("role", "alert");
    message.textContent = "Пример не загрузился. Откройте страницу через локальный HTTP-сервер по инструкции README. Данные поездки не подставлены.";
    target.append(message);
  } finally {
    target.setAttribute("aria-busy", "false");
  }
})();
