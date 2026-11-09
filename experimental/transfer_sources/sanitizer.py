"""Sanitization and defense-in-depth utilities for provider payload data.

Provider responses are untrusted input:
- No executable HTML or JavaScript.
- No prompt injection strings or role markers.
- Bounded string lengths.
- Stripped control characters.
"""

from __future__ import annotations

import html
import re

# Match HTML tags
HTML_TAG_PATTERN = re.compile(r"<[^>]+>", re.IGNORECASE)

# Match known prompt injection delimiters and role impersonation prefixes
PROMPT_INJECTION_PATTERN = re.compile(
    r"\b(?:system|assistant|user|developer|admin)\s*:\s*|"
    r"(?:\[\/?(?:inst|system|prompt)\])|"
    r"(?:<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>)|"
    r"\b(?:ignore\s+previous\s+instructions|disregard\s+all\s+prior)\b",
    re.IGNORECASE,
)

# Control characters except standard space, tab, newline
CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F-\x9F]")


def sanitize_text(text: str | None, max_length: int = 256) -> str:
    """Sanitize provider text string: defang injection markers, strip HTML, control characters."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    # Remove ASCII control characters
    text = CONTROL_CHARS_PATTERN.sub("", text)

    # Defang prompt injection markers first (before HTML stripping can swallow tags like <|im_start|>)
    text = PROMPT_INJECTION_PATTERN.sub("[DEFANGED]", text)

    # Strip HTML tags
    text = HTML_TAG_PATTERN.sub("", text)

    # Escape any remaining HTML entities
    text = html.escape(text, quote=True)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Enforce bound
    if len(text) > max_length:
        text = text[:max_length].rstrip() + "..."

    return text
