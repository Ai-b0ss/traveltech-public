"""Deterministic money arithmetic. Decimal, explicit units, half-up minor units."""

import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

MONTHS = r"(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*"
NOT_MONEY = (
    r"(?![\d.,])(?!\s*(?:" + MONTHS + r"|числ|ноч|сут|лет|год|взросл|гост|чел|номер|звезд|звёзд))"
)
NUMBER = (
    r"(-?\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?)\s*(тысяч[а-я]*|тыс\.?|[kк]\b|млн|миллион[а-я]*)?"
    + NOT_MONEY
)
CURRENCIES = {
    "RUB": r"руб|₽|\brub\b",
    "EUR": r"евро|€|\beur\b",
    "USD": r"доллар|\$|\busd\b",
    "AED": r"дирхам|\baed\b",
    "TRY": r"\bлир[аы]?\b|\btry\b",
}


def number(match):
    val_str = match[1].replace(" ", "").replace("\u00a0", "").replace(",", ".")
    value = Decimal(val_str)
    unit = match[2] or ""
    if unit.startswith(("млн", "миллион")):
        value *= 1000000
    elif unit and any(x in unit.lower() for x in ("тыс", "k", "к")):
        value *= 1000
    elif unit:
        value *= 1000
    return value


def formatted(value):
    if value <= 0 or value > Decimal("999999999999"):
        return "INVALID"
    value = value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    if value <= 0:
        return "INVALID"
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else str(value)


def resolve_budget(text, current=None, currency="RUB"):
    text = text.casefold()
    cur = Decimal(str(current)) if current is not None else None

    # Currency mismatch detection
    mentioned = {c for c, p in CURRENCIES.items() if re.search(p, text)}
    if len(mentioned) > 1:
        return "CONFLICT"
    if currency and mentioned and mentioned != {currency}:
        return "CURRENCY_MISMATCH"

    down = r"(?:снизь|снизи|уменьши|уменьшить|сбавь|опусти|скинь|сократи|урежь)"
    up = r"(?:увеличь|увеличить|подними|повысь|прибавь|добавь)"

    # Check verbs only in clauses with budget context
    clauses = re.split(r"[,;]|\s(?:и|а|но|затем|потом)\s", text)
    budget_verbs = [
        v
        for c in clauses
        if re.search(r"\d|бюджет|лимит|сумму|цену|вдвое|\bраз|%|процент", c)
        for v in re.findall(down + "|" + up, c)
    ]
    if len(budget_verbs) > 1:
        return "CONFLICT"
    if re.search(r"\b(?:или|либо)\b", text) and len(re.findall(r"\d+", text)) > 1:
        return "CONFLICT"

    targets = re.findall(
        r"(?:\bдо\b|установи(?:\s+бюджет)?|не дороже|уложись в|ровно)\s*[:=]?\s*" + NUMBER, text
    )
    target_match = re.search(
        r"(?:\bдо\b|установи(?:\s+бюджет)?|не дороже|уложись в|ровно)\s*[:=]?\s*" + NUMBER, text
    )

    def parse_t(v, unit):
        val = Decimal(v.replace(" ", "").replace("\u00a0", "").replace(",", "."))
        if unit and unit.startswith(("млн", "миллион")):
            val *= 1000000
        elif unit and any(x in unit.lower() for x in ("тыс", "k", "к")):
            val *= 1000
        elif unit:
            val *= 1000
        return str(val)

    if len({parse_t(v, unit) for v, unit in targets}) > 1:
        return "CONFLICT"

    directional = re.search(
        "("
        + down
        + "|"
        + up
        + r")\s+(?:(?:только|общий|наш|максимальный|бюджет|лимит|его|поездки|путешествия|на\s+поездку)\s+)*на\s*"
        + NUMBER
        + r"\s*(%|процент[а-я]*)?",
        text,
    )
    suffix = re.search(
        r"\bна\s*" + NUMBER + r"\s*(%|процент[а-я]*|рубл[а-я]*|rub)?\s*(меньше|больше)", text
    )
    multiplier = re.search(
        "("
        + down
        + "|"
        + up
        + r")\s+(?:бюджет\s+)?(?:вдвое|в\s*(\d+(?:[.,]\d+)?|два|три|четыре)\s*раз[а-я]*)",
        text,
    )
    result = None
    if cur is not None and re.search(
        r"оставь\s+прежний\s+бюджет|бюджет\s+(?:оставь\s+)?без\s+изменений|бюджет\s+не\s+меняй",
        text,
    ):
        if directional or multiplier:
            return "CONFLICT"
        return formatted(cur)
    if multiplier:
        if cur is None:
            return None
        raw = multiplier[2] or "2"
        factor = Decimal({"два": "2", "три": "3", "четыре": "4"}.get(raw, raw).replace(",", "."))
        if factor <= 0:
            return "INVALID"
        result = cur / factor if re.fullmatch(down, multiplier[1]) else cur * factor
    elif directional or suffix:
        if cur is None:
            return None
        if directional:
            value = Decimal(directional[2].replace(" ", "").replace("\u00a0", "").replace(",", "."))
            unit = directional[3]
            percent = directional[4]
            sign = -1 if re.fullmatch(down, directional[1]) else 1
        else:
            value = Decimal(suffix[1].replace(" ", "").replace("\u00a0", "").replace(",", "."))
            unit = suffix[2]
            percent = (
                suffix[3] if suffix[3] and ("%" in suffix[3] or "процент" in suffix[3]) else None
            )
            sign = -1 if suffix[4] == "меньше" else 1
        if value < 0:
            return "INVALID"
        if percent:
            if unit or re.search(r"(?:%|процент[а-я]*)\s*(?:руб|тыс)", text):
                return "CONFLICT"
            delta = cur * value / Decimal("100")
        else:
            if unit and unit.startswith(("млн", "миллион")):
                delta = value * 1000000
            else:
                delta = value * (1000 if unit else 1)
        result = cur + sign * delta
    elif re.search(r"%|процент", text):
        return "INVALID"
    if result is not None:
        if target_match and formatted(number(target_match)) != formatted(result):
            return "CONFLICT"
        return formatted(result)
    if target_match:
        return formatted(number(target_match))
    # An explicitly assigned trip total is absolute, not a relative delta.
    # Relative verbs are handled above, including 'increase the trip budget by'.
    generic = re.search(
        r"\b(?:бюджет|лимит)"
        r"(?:\s+(?:всей|этой|нашей))?"
        r"(?:\s+(?:поездки|путешествия|на\s+поездку|на\s+путешествие))?"
        r"(?:\s+(?:до|на|в\s+размере))?"
        r"\s*[:=]?\s*" + NUMBER,
        text,
    )
    return formatted(number(generic)) if generic else None
