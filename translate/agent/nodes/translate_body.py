"""translate_body — translate each body chunk; merge new terms into glossary."""

from __future__ import annotations

import re
import time

from ..docx_utils import postprocess
from ..glossary import clean_translation_text, extract_json_block, merge_terms
from ..prompts import build_body_messages, build_body_retry_messages
from ..state import Chunk, TranslationState


_HANGUL_RE = re.compile(r'[가-힯]')


def _contains_hangul(text: str | None) -> bool:
    return bool(text and _HANGUL_RE.search(text))


def _extract_body_text(raw: str) -> tuple[str, dict]:
    data = extract_json_block(raw) or {}
    text = clean_translation_text(data.get("text"))
    if not text:
        # Some models return plain text instead of JSON. This is acceptable,
        # including paragraph-ID-prefixed text such as "[001] The ...".
        text = clean_translation_text(raw)
    return text, data


def _apply_paragraph_id_prefix(chunk: Chunk, text: str) -> str:
    translated = postprocess(text)
    # Re-attach the head paragraph's '[NNN]' ID that chunk_body stripped before
    # sending to the LLM. If the model already emitted it, keep exactly one.
    if chunk.paragraph_id_prefix and not translated.startswith(
        chunk.paragraph_id_prefix
    ):
        translated = f"{chunk.paragraph_id_prefix} {translated.lstrip()}"
    return translated


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
    failed: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        messages = build_body_messages(
            chunk.text,
            glossary,
            equation_context=chunk.equation_context,
        )
        last_problem = ""
        for attempt in range(2):
            try:
                raw = client.complete(messages)
                text, data = _extract_body_text(raw)
                if not text:
                    last_problem = "The response did not contain usable English text."
                else:
                    translated = _apply_paragraph_id_prefix(chunk, text)
                    if _contains_hangul(translated):
                        last_problem = "The response still contains Korean/Hangul text."
                    else:
                        chunk.translation = translated
                        merge_terms(glossary, data.get("key_terms") or [])
                        break
            except Exception as exc:
                last_problem = f"The model call failed: {type(exc).__name__}: {exc}"

            if verbose:
                prefix = f"  translate_body chunk {chunk.id}: "
                suffix = " Retrying..." if attempt == 0 else ""
                print(f"{prefix}{last_problem}{suffix}")
            if attempt == 0:
                messages = build_body_retry_messages(
                    previous_messages=messages,
                    problem=last_problem,
                    chunk_text=chunk.text,
                )
        else:
            chunk.translation = ""

        if not chunk.translation:
            failed.append(chunk.id)
            if verbose:
                print(
                    f"  translate_body chunk {chunk.id}: "
                    "translation unavailable after retry"
                )

        # Heartbeat every 10 chunks (and at the end)
        if i % 10 == 0 or i == total:
            progress(f"  BODY {i}/{total}")

        if delay > 0:
            time.sleep(delay)

    if failed:
        progress(
            "WARNING: BODY translation unavailable for chunk(s): "
            + ", ".join(failed)
            + ". Those source paragraphs will remain unchanged unless the final Korean-ratio check aborts the file."
        )
    return {"chunks_body": chunks, "glossary": glossary}
