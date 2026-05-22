"""translate_abstract — single LLM call for the whole abstract."""

from __future__ import annotations

from ..docx_utils import postprocess
from ..glossary import clean_translation_text, extract_json_block, merge_terms
from ..prompts import build_abstract_messages, build_simple_abstract_messages
from ..state import TranslationState
from ..validation import translation_problem


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
    messages = build_abstract_messages(chunk.text, glossary)
    last_problem = ""
    for attempt in range(4):
        try:
            raw = client.complete(messages)
            data = extract_json_block(raw) or {}
            text = clean_translation_text(data.get("text")) or clean_translation_text(raw)
            problem = translation_problem(text)
            if problem:
                last_problem = problem
            else:
                translated = postprocess(text)
                problem = translation_problem(translated)
                if problem:
                    last_problem = problem
                else:
                    chunk.translation = translated
                    merge_terms(glossary, data.get("key_terms") or [])
                    break
        except Exception as exc:
            last_problem = f"The model call failed: {type(exc).__name__}: {exc}"

        if verbose:
            suffix = " Retrying..." if attempt < 3 else ""
            print(f"  translate_abstract: {last_problem}{suffix}")
        if attempt == 2:
            messages = build_simple_abstract_messages(chunk.text, glossary)
    else:
        raise RuntimeError(
            "ABSTRACT translation failed after retries: " + (last_problem or "unknown error")
        )

    return {"chunks_abstract": chunks, "glossary": glossary}
