"""Shared failures for optional FX and POI enrichment. No retries."""


class EnrichmentError(ValueError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")
