"""Terminology glossary — extracted from each LLM call to keep cross-section consistency."""

from __future__ import annotations

import json
import re


_JSON_RE = re.compile(r'(\{.*\}|\[.*\])', re.DOTALL)

# Placeholder text patterns from our JSON-schema templates ('<English translation>',
# '<Korean term>', '<...>' generic). When the LLM echoes our schema verbatim
# instead of filling it in, these strings come back as if they were real
# translations. We treat them as empty so callers fall back to the failure path.
_PLACEHOLDER_LITERAL_RE = re.compile(r'^\s*<[^<>]+>\s*$')
_JSONISH_RESPONSE_RE = re.compile(
    r'^\s*(?:```(?:json)?\s*)?(?:\{|\[(?!\d{1,5}\]))|'
    r'"\s*(?:text|key_terms)\s*"|\b(?:text|key_terms)\s*:',
    re.IGNORECASE | re.DOTALL,
)
_EMPTY_INPUT_RESPONSE_RE = re.compile(
    r'\b(?:empty|blank|no|missing)\s+'
    r'(?:text|input|source|content|korean|paragraph|claim)\b|'
    r'\b(?:text|input|source|content|korean|paragraph|claim)\s+'
    r'(?:is|was|appears\s+to\s+be)?\s*(?:empty|blank|missing)\b|'
    r'\bnothing\s+to\s+translate\b',
    re.IGNORECASE,
)
_META_TRANSLATION_RESPONSE_RE = re.compile(
    r"\b(?:i\s+am|i'm|i\s+was|i'll|i\s+will)\s+"
    r"(?:ready|happy|unable|sorry)\b.*\btranslate\b|"
    r"\bplease\s+(?:provide|send|share)\b.*\b(?:text|content|korean)\b|"
    r"\b(?:no|without)\s+(?:source\s+)?(?:text|content|korean)\s+"
    r"(?:was\s+)?(?:provided|included|given)\b",
    re.IGNORECASE | re.DOTALL,
)
_LEADING_MARKDOWN_LABEL_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*"
    r"(?:"
    r"translation|english\s+translation|translated\s+text|"
    r"uspto(?:[-\s]+style)?\s+translation|"
    r"abstract(?:\s*\(\s*uspto(?:[-\s]+style)?\s*\))?|"
    r"claim\s+translation|body\s+translation"
    r")"
    r"\s*(?:[:：])?\s*(?:\*\*|__)?\s*(?:\n+|$)",
    re.IGNORECASE,
)
_LEADING_PREFACE_RE = re.compile(
    r"^\s*(?:Here(?:'s| is)\s+)?(?:the\s+)?"
    r"(?:formal\s+)?(?:USPTO(?:[-\s]+style)?\s+)?"
    r"(?:English\s+)?translation\s+(?:is\s+)?[:：]\s*",
    re.IGNORECASE,
)
_MARKDOWN_RULE_RE = re.compile(
    r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE,
)
_MARKDOWN_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*|__([^_\n]+?)__")
_MARKDOWN_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)

_PLACEHOLDER_LITERALS = frozenset({
    "<english translation>",
    "<english claim sentence>",
    "<english abstract>",
    "<english>",
    "<english term>",
    "<korean term>",
    "<korean>",
    "...",
    "…",
})


def is_placeholder_value(text: str | None) -> bool:
    """True when ``text`` is an LLM-echoed JSON-schema placeholder rather than
    real translation content. Catches both the exact literal forms used in
    our prompts and any single-token '<...>' placeholder."""
    if not text:
        return False
    s = text.strip()
    if not s:
        return False
    if s.lower() in _PLACEHOLDER_LITERALS:
        return True
    if _PLACEHOLDER_LITERAL_RE.match(s):
        return True
    return False


def clean_text(value: str | None) -> str:
    """Return ``value`` if it looks like real content, else empty string."""
    if value is None:
        return ""
    return "" if is_placeholder_value(value) else value.strip()


def clean_translation_text(value: str | None) -> str:
    """Return only plain translated text, never schema/JSON payload text."""
    text = clean_text(value)
    if not text:
        return ""
    if _JSONISH_RESPONSE_RE.search(text):
        return ""
    text = _strip_translation_markdown(text)
    if not text:
        return ""
    if _JSONISH_RESPONSE_RE.search(text):
        return ""
    if len(text) <= 700 and _META_TRANSLATION_RESPONSE_RE.search(text):
        return ""
    if len(text) <= 240 and _EMPTY_INPUT_RESPONSE_RE.search(text):
        return ""
    return text


def _strip_translation_markdown(text: str) -> str:
    """Remove presentation artifacts that LLMs sometimes emit as text."""
    text = text.strip()
    previous = None
    while previous != text:
        previous = text
        text = _LEADING_MARKDOWN_LABEL_RE.sub("", text, count=1).lstrip()
        text = _LEADING_PREFACE_RE.sub("", text, count=1).lstrip()
    text = _MARKDOWN_RULE_RE.sub("", text)
    text = _MARKDOWN_HEADING_RE.sub("", text)
    text = _MARKDOWN_BOLD_RE.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_json_block(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                return None
    return None


def merge_terms(glossary: dict[str, str], new_terms: list[dict]) -> dict[str, str]:
    """Merge new {ko, en} terms into the glossary. First-seen translation wins.

    Filters entries where ko or en is empty or a schema placeholder
    ('<Korean term>', '<English term>', '<...>') so the LLM echoing our
    template can't pollute the glossary."""
    if not isinstance(new_terms, list):
        return glossary
    for item in new_terms:
        if not isinstance(item, dict):
            continue
        ko = clean_text(item.get("ko"))
        en = clean_text(item.get("en"))
        if ko and en and ko not in glossary:
            glossary[ko] = en
    return glossary


def format_for_prompt(glossary: dict[str, str], limit: int = 80) -> str:
    """Render the glossary as a compact list for the system prompt."""
    if not glossary:
        return "(none yet)"
    items = list(glossary.items())[:limit]
    return "\n".join(f"  {ko} → {en}" for ko, en in items)
