"""Validation helpers for LLM translation output."""

from __future__ import annotations

import re


_HANGUL_RE = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uA960-\uA97F\uAC00-\uD7AF\uD7B0-\uD7FF]")
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


_PAREN_REF_RE = re.compile(
    r"[\(\[（［](?P<ref>(?:[A-Z]{2,8}|(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{1,16}))[\)\]）］]"
)
_COMPACT_REF_RE = re.compile(
    r"(?P<prefix>[A-Z]{1,6}\d?)[\(（](?P<inner>[0-9][A-Za-z0-9_-]{0,5})[\)）]"
)
_EQUATION_MARKER_RE = re.compile(r"\[EQUATION(?:_\d+)?\]")


def source_reference_tokens(source: str | None) -> set[str]:
    """Reference tokens that must survive translation.

    Korean patent documents often use both digit-bearing reference numerals
    (100, 100a, S10, GR(1)) and pure-letter reference characters (LD, GC).
    The earlier validator only tracked digit-bearing forms, which let pure
    letter references disappear or remain mishandled.
    """
    if not source:
        return set()
    refs: set[str] = set()
    for m in _PAREN_REF_RE.finditer(source):
        ref = m.group("ref")
        if ref.upper().startswith("EQUATION"):
            continue
        refs.add(ref)
    for m in _COMPACT_REF_RE.finditer(source):
        refs.add(m.group("prefix") + m.group("inner"))
    return refs


def _english_has_token(english: str, token: str) -> bool:
    compact = re.sub(r"[()\[\]（）［］\s]", "", english or "")
    if token in compact:
        return True
    return re.search(
        rf"(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])",
        english or "",
        re.IGNORECASE,
    ) is not None


def coverage_problem(source: str | None, translation: str | None) -> str:
    """Return a deterministic source→translation coverage problem, or ''."""
    if not source or not translation:
        return ""
    missing_refs = sorted(
        ref for ref in source_reference_tokens(source)
        if not _english_has_token(translation, ref)
    )
    if missing_refs:
        shown = ", ".join(missing_refs[:12])
        return f"The translation omits source reference numeral/character(s): {shown}."

    src_eq = _EQUATION_MARKER_RE.findall(source)
    dst_eq = _EQUATION_MARKER_RE.findall(translation)
    if src_eq != dst_eq:
        return "The translation does not preserve [EQUATION] marker count/order."

    # Gross omission/summarization guard. English patent translation is rarely
    # much shorter than Korean source text. Keep this conservative so concise
    # legitimate translations pass, but obvious paragraph/sentence drops retry
    # instead of being written over the source paragraph.
    src_visible = _EQUATION_MARKER_RE.sub("", source)
    src_len = len(re.sub(r"\s+", "", src_visible))
    dst_len = len(re.sub(r"\s+", "", translation))
    if src_len >= 120 and dst_len < int(src_len * 0.45):
        return "The translation is suspiciously short relative to the source and may omit content."
    return ""


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
