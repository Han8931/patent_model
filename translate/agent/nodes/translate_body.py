"""translate_body — translate each body chunk; merge new terms into glossary."""

from __future__ import annotations

import time

from ..docx_utils import postprocess
from ..glossary import extract_json_block, merge_terms
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

    progress(f"Translating BODY ({len(chunks)} chunks)…")
    for i, chunk in enumerate(chunks, 1):
        if verbose:
            print(f"  BODY {i}/{len(chunks)} ({chunk.section})")
        try:
            raw = client.complete(build_body_messages(chunk.text, glossary))
            data = extract_json_block(raw) or {}
            text = (data.get("text") or "").strip()
            if not text:
                # Treat as fallback: keep raw text as the translation candidate
                text = raw.strip()
            chunk.translation = postprocess(text)
            merge_terms(glossary, data.get("key_terms") or [])
        except Exception as exc:
            if verbose:
                print(f"  translate_body chunk {chunk.id} failed: {exc}")
            chunk.translation = chunk.text  # leave Korean as fallback marker

        if delay > 0:
            time.sleep(delay)

    return {"chunks_body": chunks, "glossary": glossary}
