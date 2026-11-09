"""Expire reconstructible public caches; compact bodies without deleting receipts."""

import hashlib, json, os, re, time
from pathlib import Path


def receipt_metadata(record):
    out = {k: v for k, v in record.items() if k not in ("data", "request")}
    if isinstance(out.get("source"), str):
        out["source"] = out["source"].split("?", 1)[0]
    for name in ("data", "request"):
        if name in record:
            out[name + "_sha256"] = hashlib.sha256(
                json.dumps(
                    record[name], sort_keys=True, ensure_ascii=False, separators=(",", ":")
                ).encode()
            ).hexdigest()
    return out


def cleanup_public(state, at=None):
    state = Path(state)
    at = time.time() if at is None else at
    removed = 0
    for p in state.glob("*.json"):
        if p.is_symlink() or not re.fullmatch(r"[a-f0-9]{64}(?:\.failed)?\.json", p.name):
            continue
        try:
            row = json.loads(p.read_text(encoding="utf-8"))
            deadline = (
                row.get("retry_after_unix")
                if p.name.endswith(".failed.json")
                else row.get("unix", at) + row.get("cache_ttl_seconds", 0)
            )
            if isinstance(deadline, (int, float)) and at > deadline and at - p.stat().st_mtime > 60:
                p.unlink()
                removed += 1
        except (OSError, ValueError, TypeError):
            continue
    journal = state / "public_receipts.jsonl"
    # Atomic compaction retains every ID/status/hash, including unknown failures.
    # Never rotate away accounting history or parse a partial line as no effect.
    if journal.exists() and not journal.is_symlink() and journal.stat().st_size > 1000000:
        temp = state / "public_receipts.compact.tmp"
        try:
            with (
                journal.open(encoding="utf-8") as source,
                temp.open("w", encoding="utf-8") as target,
            ):
                for line in source:
                    row = json.loads(line)
                    target.write(
                        json.dumps(receipt_metadata(row), ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp, journal)
        except (OSError, ValueError, TypeError):
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    return removed
