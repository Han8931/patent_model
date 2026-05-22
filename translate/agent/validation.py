"""Validation helpers for LLM translation output."""

from __future__ import annotations

import re


_HANGUL_RE = re.compile(r"[가-힯]")
_MARKDOWN_RE = re.compile(
    r"^\s*```|```\s*$|^\s{0,3}#{1,6}\s+|^\s*[-*]\s+|\|.+\|",
    re.MULTILINE,
)
_JSON_ARTIFACT_RE = re.compile(
    r'^\s*(?:```(?:json)?\s*)?(?:\{|\[(?!\d{1,5}\]))|'
    r'"\s*(?:text|key_terms|translation|ko|en)\s*"|'
    r"\b(?:text|key_terms|translation)\s*:",
    re.IGNORECASE | re.DOTALL,
)
_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:<[^<>]+>|\.\.\.|…|N/?A|none|null)\s*$",
    re.IGNORECASE,
)
_NO_RESPONSE_RE = re.compile(
    r"\b(?:no|nothing|empty|missing)\s+"
    r"(?:text|input|source|content|korean|paragraph|claim|translation)\b|"
    r"\b(?:cannot|can't|unable\s+to)\s+(?:translate|process)\b",
    re.IGNORECASE,
)


def translation_problem(text: str | None, *, require_no_hangul: bool = True) -> str:
    """Return a concrete output-quality problem, or '' if usable."""
    if text is None:
        return "The model returned no response."
    value = text.strip()
    if not value:
        return "The model returned empty output."
    if _PLACEHOLDER_RE.match(value):
        return "The model returned placeholder text instead of a translation."
    if _NO_RESPONSE_RE.search(value) and len(value) <= 300:
        return "The model returned a refusal/no-input message instead of a translation."
    if _MARKDOWN_RE.search(value):
        return "The model returned markdown formatting or a table."
    if _JSON_ARTIFACT_RE.search(value):
        return "The model returned JSON/schema artifacts instead of plain translated text."
    if require_no_hangul and _HANGUL_RE.search(value):
        return "The translation still contains Korean/Hangul text."
    return ""
