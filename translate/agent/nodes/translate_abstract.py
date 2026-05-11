"""translate_abstract — single LLM call for the whole abstract."""

from __future__ import annotations

from ..docx_utils import postprocess
from ..glossary import clean_text, extract_json_block, merge_terms
from ..prompts import build_abstract_messages
from ..state import TranslationState


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
    try:
        raw = client.complete(build_abstract_messages(chunk.text, glossary))
        data = extract_json_block(raw) or {}
        text = clean_text(data.get("text")) or clean_text(raw)
        if text:
            chunk.translation = postprocess(text)
        else:
            if verbose:
                print("  translate_abstract: empty/placeholder output, keeping Korean")
            chunk.translation = chunk.text
        merge_terms(glossary, data.get("key_terms") or [])
    except Exception as exc:
        if verbose:
            print(f"  translate_abstract failed: {exc}")
        chunk.translation = chunk.text

    return {"chunks_abstract": chunks, "glossary": glossary}
