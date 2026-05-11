"""translate_body — translate each body chunk; merge new terms into glossary."""

from __future__ import annotations

import time

from ..docx_utils import postprocess
from ..glossary import clean_translation_text, extract_json_block, merge_terms
from ..prompts import build_body_messages
from ..state import TranslationState


def translate_body(state: TranslationState) -> dict:
    chunks = state.get("chunks_body", [])
    if not chunks:
        return {}

    client = state["client"]
    glossary = dict(state.get("glossary", {}))
    delay = state.get("delay", 0.0)
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)

    total = len(chunks)
    progress(f"Translating BODY ({total} chunks)…")
    for i, chunk in enumerate(chunks, 1):
        try:
            raw = client.complete(
                build_body_messages(
                    chunk.text,
                    glossary,
                    equation_context=chunk.equation_context,
                )
            )
            data = extract_json_block(raw) or {}
            text = clean_translation_text(data.get("text"))
            if not text:
                # Don't fall back to raw if the LLM just echoed our schema —
                # writing '<English translation>' verbatim to the docx is worse
                # than leaving the Korean visible.
                fallback = clean_translation_text(raw)
                text = fallback
            if text:
                chunk.translation = postprocess(text)
            else:
                if verbose:
                    print(f"  translate_body chunk {chunk.id}: empty/placeholder output, keeping Korean")
                chunk.translation = chunk.text
            merge_terms(glossary, data.get("key_terms") or [])
        except Exception as exc:
            if verbose:
                print(f"  translate_body chunk {chunk.id} failed: {exc}")
            chunk.translation = chunk.text  # leave Korean as fallback marker

        # Heartbeat every 10 chunks (and at the end)
        if i % 10 == 0 or i == total:
            progress(f"  BODY {i}/{total}")

        if delay > 0:
            time.sleep(delay)

    return {"chunks_body": chunks, "glossary": glossary}
