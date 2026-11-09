"""Fail-closed InkStone model pricing. Supports reviewed split or merged tables."""

from decimal import Decimal, InvalidOperation
import re
from provider_transport import Failure

LABELS = {
    "qwen3.8-27b": "Qwen3.8-27B",
    "glm-5.3": "GLM-5.3",
    "minimax-m3": "MiniMax-M3",
    "deepseek-v4-flash-0731": "DeepSeek-V4-Flash-0731",
    "deepseek-v4-flash-vision": "DeepSeek-V4-Flash-Vision",
    "deepseek-v4-pro-0813": "DeepSeek-V4-Pro-0813",
    "kimi-k2.6": "Kimi-K2.6",
    "Atria-Dawn-Preview": "Atria-Dawn-Preview",
    "Agents-A1": "Agents-A1",
    "intern-s2": "书生-S2",
}


def section(doc, title):
    if not isinstance(doc, str) or not 100 < len(doc) < 800000:
        raise Failure("INKSTONE_PRICE_DOC_INVALID")
    lines = doc.replace("\r\n", "\n").splitlines()
    candidates = [i for i, v in enumerate(lines) if v.strip() == title]
    if len(candidates) != 1:
        raise Failure("INKSTONE_TABLE_HEADING_AMBIGUOUS")
    data = []
    for line in lines[candidates[0] + 1 :]:
        if re.match(r"^##\s+", line):
            break
        if line.strip().startswith("|"):
            data.append([x.strip() for x in line.strip().strip("|").split("|")])
    return data


def prices(values):
    try:
        result = tuple(Decimal(x) for x in values)
    except (ValueError, InvalidOperation):
        raise Failure("INKSTONE_PRICE_UNREADABLE")
    if len(result) != 3 or any(not x.is_finite() or x < 0 or x > 100000 for x in result):
        raise Failure("INKSTONE_PRICE_INVALID")
    return result


def extract(doc, model):
    if model not in LABELS:
        raise Failure("INKSTONE_MODEL_UNREVIEWED")
    lines = doc.splitlines()
    merged = "## 模型列表及价格" in [x.strip() for x in lines]
    if merged:
        matches = [r for r in section(doc, "## 模型列表及价格") if len(r) > 1 and r[1] == model]
        if len(matches) != 1 or len(matches[0]) != 9:
            raise Failure("INKSTONE_MODEL_PRICE_ROW_AMBIGUOUS")
        row = matches[0]
        name, id, api = row[:3]
        rate = prices(row[5:8])
    else:
        products = [r for r in section(doc, "## 模型列表") if len(r) > 1 and r[1] == model]
        options = [r for r in section(doc, "## 墨点价格") if r and r[0] == LABELS[model]]
        if len(products) != 1 or len(options) != 1 or len(products[0]) != 5 or len(options[0]) != 5:
            raise Failure("INKSTONE_MODEL_PRICE_ROW_AMBIGUOUS")
        name, id, api = products[0][:3]
        rate = prices(options[0][1:4])
    if name != LABELS[model] or id != model or "OpenAI ChatCompletions" not in api:
        raise Failure("INKSTONE_MODEL_CAPABILITY_CHANGED")
    return rate


def verify_model_price(reviewed_doc, live_doc, model, code_rates):
    reviewed = extract(reviewed_doc, model)
    live = extract(live_doc, model)
    if reviewed != live:
        raise Failure("INKSTONE_SELECTED_MODEL_PRICE_CHANGED")
    if live[1:] != tuple(Decimal(str(v)) for v in code_rates):
        raise Failure("INKSTONE_CODE_PRICE_MISMATCH")
    return {
        "model": model,
        "cache_hit": str(live[0]),
        "uncached_input": str(live[1]),
        "output": str(live[2]),
        "same_as_reviewed": True,
    }
