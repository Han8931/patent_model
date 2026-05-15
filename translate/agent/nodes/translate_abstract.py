"""translate_abstract — single LLM call for the whole abstract."""

from __future__ import annotations

import re

from ..docx_utils import postprocess
from ..glossary import clean_translation_text, extract_json_block, merge_terms
from ..prompts import build_abstract_messages
from ..state import TranslationState
from .translate_claims import _strip_markdown


_HANGUL_RE = re.compile(r"[가-힯]")


def _contains_hangul(text: str | None) -> bool:
    return bool(text and _HANGUL_RE.search(text))


def _one_call(client, messages: list[dict]) -> tuple[str, dict]:
    """Single LLM call → (cleaned English text, parsed key_terms dict)."""
    raw = client.complete(messages)
    data = extract_json_block(raw) or {}
    text = clean_translation_text(data.get("text")) or clean_translation_text(raw)
    if text:
        text = _strip_markdown(text)
    return text, data


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
        messages = build_abstract_messages(chunk.text, glossary)
        text, data = _one_call(client, messages)

        # Retry once if the first response is empty or still contains Korean.
        # Falling back to chunk.text (the Korean source) would leak Korean
        # into the docx as if it were a translation — never do that.
        if not text or _contains_hangul(text):
            problem = (
                "empty/placeholder response"
                if not text else "response still contained Korean characters"
            )
            if verbose:
                print(f"  translate_abstract: retry ({problem})")
            retry_messages = messages + [{
                "role": "user",
                "content": (
                    "Your previous response was unusable. "
                    f"Problem: {problem}. "
                    "Translate the Korean abstract above into English. "
                    "Output English only — no Korean characters anywhere, "
                    "no markdown, no commentary."
                ),
            }]
            text, data = _one_call(client, retry_messages)

        if text and not _contains_hangul(text):
            chunk.translation = postprocess(text)
            merge_terms(glossary, data.get("key_terms") or [])
        elif verbose:
            print(
                "  translate_abstract: still empty/Korean after retry, "
                "leaving source paragraph untouched"
            )
    except Exception as exc:
        if verbose:
            print(f"  translate_abstract failed: {exc}")

    return {"chunks_abstract": chunks, "glossary": glossary}
