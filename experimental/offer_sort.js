/* One ordering for the main list and detailed supplier rates. */
function mvpSortOffers(offers, mode = "price") {
  const items = Array.isArray(offers) ? offers.slice() : [];
  if (mode === "supplier" || mode === "verified") return items;
  if (mode === "name")
    return items.sort((a, b) =>
      String(a.hotel_name || "").localeCompare(
        String(b.hotel_name || ""), "ru", { sensitivity: "base" },
      ),
    );
  const direction = mode === "price-desc" ? -1 : 1;
  const quote = (offer) => {
    if (typeof fxQuote === "function") {
      const q = fxQuote(offer);
      if (q?.comparable && Number.isFinite(q.amount) && q.amount >= 0)
        return q.amount;
    }
    return null;
  };
  return items.sort((a, b) => {
    const pa = quote(a), pb = quote(b);
    if (pa !== null && pb !== null) return direction * (pa - pb);
    if (pa !== null) return -1;
    if (pb !== null) return 1;
    // Without verified FX, only native prices in the SAME currency can sort.
    if (a.currency === b.currency) {
      const va = Number(a.total), vb = Number(b.total);
      if (Number.isFinite(va) && Number.isFinite(vb))
        return direction * (va - vb);
    }
    return 0;
  });
}
window.traveltechSortOffers = mvpSortOffers;
