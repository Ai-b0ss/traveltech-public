"""CPU-only performance measurements; no claim about live supplier/model latency."""

import json, time, statistics, platform
from dataclasses import replace, asdict
from pathlib import Path
from test_core import BASE, REQ, NOW
from travel_core import Engine, ListProvider, Request, Guests
from adapters import ArchivedTourMind

OUT = Path(__file__).parent / "results"
OUT.mkdir(exist_ok=True)
stats = []
for n in (10, 100, 1000, 10000):
    rows = [
        replace(BASE, hotel_id="h" + str(i), rate_id="rate" + str(i), amount=str(1 + i % 500))
        for i in range(n)
    ]
    engine = Engine(ListProvider(rows))
    for _ in range(3):
        engine.search(REQ, NOW)
    samples = []
    for _ in range(31):
        start = time.perf_counter_ns()
        r = engine.search(REQ, NOW)
        samples.append((time.perf_counter_ns() - start) / 1e6)
        assert r["scanned"] == n and not r["issues"] and not r["truncated"]
    ordered = sorted(samples)
    stats.append(
        {
            "rows": n,
            "repetitions": len(samples),
            "median_ms": statistics.median(samples),
            "p95_ms": ordered[29],
            "min_ms": min(samples),
            "max_ms": max(samples),
            "samples_ms": samples,
            "llm_calls": 0,
            "http_calls": 0,
        }
    )
rows = json.loads((Path(__file__).parent / "fixtures/tourmind_36.json").read_text())
req = Request("Dubai", "AE", "2026-10-26", "2026-10-28", (Guests(2),), "100000", "CNY")
r = Engine(ArchivedTourMind(rows)).search(req, NOW)
result = {
    "environment": {
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
    },
    "scope": "LOCAL_CPU_ONLY_SYNTHETIC_DATA; not a hotel API/LLM/browser benchmark",
    "benchmark": stats,
    "external_usage_cost_this_benchmark": 0,
    "overall_production_cost": "UNKNOWN",
    "archive_result": r,
    "production_latency": "UNKNOWN",
    "image_analysis": "NOT_IMPLEMENTED",
    "review_analysis": "NOT_IMPLEMENTED",
}
(OUT / "benchmark.json").write_text(
    json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
        default=lambda x: asdict(x) if hasattr(x, "__dataclass_fields__") else str(x),
    )
)
print(
    json.dumps(
        {
            "benchmarks": [{k: v for k, v in x.items() if k != "samples_ms"} for x in stats],
            "archive_rates": len(r["decisions"]),
            "archive_statuses": sorted(set(d.status for d in r["decisions"])),
        },
        ensure_ascii=False,
        indent=2,
    )
)
