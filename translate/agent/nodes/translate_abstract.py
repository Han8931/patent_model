"""translate_abstract — single LLM call for the whole abstract.

Uses the slim ABSTRACT_SYSTEM prompt: plain-text response plus an optional
``===== GLOSSARY =====`` trailer of new Korean→English term pairs. Trailer
pairs extend ``state.glossary`` via ``setdefault`` so claim-derived terms
are never overwritten.
"""

from __future__ import annotations

import re

from ..docx_utils import postprocess
from ..prompts import build_abstract_messages, parse_translation_dual_shape
from ..state import TranslationState


_HANGUL_RE = re.compile(r"[가-힯]")


def _contains_hangul(text: str | None) -> bool:
    return bool(text and _HANGUL_RE.search(text))


def translate_abstract(state: TranslationState) -> dict:
    chunks = state.get("chunks_abstract", [])
    if not chunks:
        return {}

    client = state["client"]
    glossary = dict(state.get("glossary", {}))
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)

    chunk = chunks[0]
    progress("ABSTRACT")
    chunk.translation = ""
    try:
        raw = client.complete(build_abstract_messages(chunk.text, glossary))
        text, new_terms = parse_translation_dual_shape(raw)
        if text and not _contains_hangul(text):
            chunk.translation = postprocess(text)
            for ko, en in new_terms.items():
                glossary.setdefault(ko, en)
        elif verbose:
            problem = "empty/placeholder output" if not text else "still contains Korean"
            print(f"  translate_abstract: {problem}; leaving source paragraph untouched")
    except Exception as exc:
        if verbose:
            print(f"  translate_abstract failed: {exc}")

    return {"chunks_abstract": chunks, "glossary": glossary}
