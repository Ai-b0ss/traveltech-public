> Исторический прототип / отчёт. Основной запуск и текущий статус: [документация](../docs/README.md).

# Stage 3: captured live evidence and offline replay

Read REPORT_RU.md for findings and limitations. Python 3.12 standard library; no packages required. The base/ directory preserves stage 2 source byte for byte.

## Offline verification

Run from this directory:

```bash
python3 -m unittest -v test_stage3
python3 replay_flow.py
```

Run the original regression tests from the base/ directory:

```bash
python3 -m unittest -v test_core
```

These commands do not call the supplier or a language model. Tests use saved supplier replies and synthetic transport failures. Some test result files and connected_replay.json are rewritten when commands run; the delivered manifest describes the original packaged files.

## Files and boundaries

- current_adapter.py normalizes captured responses; it is not a realtime connector.
- evidence.py verifies normalized evidence; it does not extract claims or fetch guest reviews.
- session.py handles in-memory revisioned refinements and undo, not persistent authenticated sessions.
- fee_disclosure.py preserves mandatory/optional fee disclosures without speculative totals.
- results/actual_http_call_ledger.json lists exactly 20 genuine API calls, including one HTTP 504.
- visual_inspection/ contains two supplier room photos, provenance and human inspection notes.
- base/ retains the stage 2 deterministic core and archived fixtures.

probe_sources.py, probe_selected.py and probe_occupancy.py are LIVE NETWORK research scripts. Do not run them as offline tests. They use allowlisted read-only public supplier operations, limited concurrency, response sizes and timeouts. No booking or payment operations are implemented. Saved rates describe the recorded dates and guests; they do not certify current price, inventory or bookability. No reviews or automatic image-analysis pipeline were connected. Raw replies may include expiring supplier URLs; keep the research bundle private rather than publishing these URLs.

verification.json and manifest.json record package verification and SHA-256 hashes. Synthetic failure captures are explicitly marked and excluded from the real call ledger.
