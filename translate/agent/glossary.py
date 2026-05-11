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
    r'^\s*(?:```(?:json)?\s*)?[\{\[]|"\s*(?:text|key_terms)\s*"|'
    r'\b(?:text|key_terms)\s*:',
    re.IGNORECASE | re.DOTALL,
)

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
    return text


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
