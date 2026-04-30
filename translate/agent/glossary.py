"""Terminology glossary — extracted from each LLM call to keep cross-section consistency."""

from __future__ import annotations

import json
import re


_JSON_RE = re.compile(r'(\{.*\}|\[.*\])', re.DOTALL)


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
    """Merge new {ko, en} terms into the glossary. First-seen translation wins."""
    if not isinstance(new_terms, list):
        return glossary
    for item in new_terms:
        if not isinstance(item, dict):
            continue
        ko = (item.get("ko") or "").strip()
        en = (item.get("en") or "").strip()
        if ko and en and ko not in glossary:
            glossary[ko] = en
    return glossary


def format_for_prompt(glossary: dict[str, str], limit: int = 80) -> str:
    """Render the glossary as a compact list for the system prompt."""
    if not glossary:
        return "(none yet)"
    items = list(glossary.items())[:limit]
    return "\n".join(f"  {ko} → {en}" for ko, en in items)
