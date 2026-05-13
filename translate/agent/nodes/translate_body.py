"""translate_body — translate each body chunk; merge new terms into glossary."""

from __future__ import annotations

import re
import time

from ..docx_utils import postprocess
from ..glossary import clean_translation_text, extract_json_block, merge_terms
from ..prompts import (
    build_body_messages,
    build_body_retry_messages,
    build_clause_messages,
    build_segment_messages,
)
from ..paragraph_translator import (
    chunk_has_math,
    translate_chunk_per_paragraph,
)
from ..sentence_translator import (
    needs_per_segment_translation,
    translate_chunk_by_sentence,
)
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
    font = state.get("font", "Times New Roman")
    delay = state.get("delay", 0.0)
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)

    # Per-paragraph in-place lookup needs an index-addressable record list,
    # not the flat list `state['records']` provides (its index field is the
    # docx paragraph index, not the list position). Build it once here.
    records = state.get("records", [])
    by_index = {r.index: r for r in records}
    indexed_records: list = (
        [None] * (max(by_index) + 1) if by_index else []
    )
    for idx, r in by_index.items():
        indexed_records[idx] = r

    total = len(chunks)
    progress(f"Translating BODY ({total} chunks)…")
    failed: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        # Equation-bearing chunks (any paragraph in the chunk has inline math
        # or is a standalone <m:oMath> paragraph) take the per-paragraph
        # in-place path: each source paragraph is translated independently and
        # written back into its own <w:p>, with <m:oMath> elements left
        # untouched at their source XML position. The write node skips these
        # chunks because chunk.applied_in_place is set.
        if chunk_has_math(chunk, indexed_records):
            try:
                applied = translate_chunk_per_paragraph(
                    chunk,
                    records=indexed_records,
                    client=client,
                    glossary=glossary,
                    font_name=font,
                    build_segment_messages=build_segment_messages,
                    build_clause_messages=build_clause_messages,
                    verbose=verbose,
                )
            except Exception as exc:
                if verbose:
                    print(
                        f"  translate_body chunk {chunk.id} (per-paragraph): "
                        f"{type(exc).__name__}: {exc}"
                    )
                failed.append(chunk.id)
            else:
                if verbose:
                    print(
                        f"  translate_body chunk {chunk.id}: "
                        f"per-paragraph applied to {applied} paragraph(s)"
                    )
            if i % 10 == 0 or i == total:
                progress(f"  BODY {i}/{total}")
            if delay > 0:
                time.sleep(delay)
            continue

        # Chunks containing opaque markers ([EQUATION_N] or inline [NNN]
        # paragraph IDs from <w:br> line breaks) take the sentence-level path:
        # one LLM call per text segment and per per-symbol legend clause. The
        # markers never go through a single combined translation, so they
        # can't drift out of position or get glued together at the start.
        # Pure-prose chunks keep the cheaper single call.
        if needs_per_segment_translation(chunk.text):
            try:
                en = translate_chunk_by_sentence(
                    chunk.text,
                    client,
                    glossary,
                    build_segment_messages=build_segment_messages,
                    build_clause_messages=build_clause_messages,
                )
            except Exception as exc:
                en = None
                if verbose:
                    print(
                        f"  translate_body chunk {chunk.id} (sentence-level): "
                        f"{type(exc).__name__}: {exc}"
                    )
            if en:
                chunk.translation = _apply_paragraph_id_prefix(chunk, en)
            else:
                chunk.translation = ""
            if not chunk.translation:
                failed.append(chunk.id)
            if i % 10 == 0 or i == total:
                progress(f"  BODY {i}/{total}")
            if delay > 0:
                time.sleep(delay)
            continue

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
