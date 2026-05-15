"""translate_body — translate each body chunk; merge new terms into glossary."""

from __future__ import annotations

import re
import time

from ..docx_utils import postprocess
from ..glossary import clean_translation_text
from ..prompts import (
    build_body_simple_messages,
    build_clause_messages,
    build_segment_messages,
    parse_body_simple_response,
)
from .translate_claims import _strip_markdown
from ..paragraph_translator import (
    chunk_needs_per_paragraph,
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


def _apply_paragraph_id_prefix(chunk: Chunk, text: str) -> str:
    # The minimal body prompt sometimes returns markdown decorations
    # (**bold**, leading bullets, `---` rules). Strip those before
    # postprocess so the docx output is clean prose.
    text = _strip_markdown(text)
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
        if chunk_needs_per_paragraph(chunk, indexed_records):
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
                translated = _apply_paragraph_id_prefix(chunk, en)
                if _contains_hangul(translated):
                    chunk.translation = ""
                    if verbose:
                        print(
                            f"  translate_body chunk {chunk.id} (sentence-level): "
                            "response still contains Korean after retry; "
                            "leaving source paragraph untouched"
                        )
                else:
                    chunk.translation = translated
            else:
                chunk.translation = ""
            if not chunk.translation:
                failed.append(chunk.id)
            if i % 10 == 0 or i == total:
                progress(f"  BODY {i}/{total}")
            if delay > 0:
                time.sleep(delay)
            continue

        # Pure-prose chunks: single bulk-simple call, plain text response.
        # The claim translator already seeded `glossary`; the simple prompt
        # injects it into the system message and asks the model to append a
        # '===== GLOSSARY =====' block of any NEW terms it introduced. We
        # merge those back with `setdefault` so claim-derived terms are
        # NEVER overwritten by description-only translations — the body can
        # only extend the glossary, not redefine it.
        messages = build_body_simple_messages(chunk.text, glossary)
        chunk.translation = ""
        try:
            raw = client.complete(messages)
            translation_text, new_terms = parse_body_simple_response(raw)
            text = clean_translation_text(translation_text)
            translated = _apply_paragraph_id_prefix(chunk, text) if text else ""

            # Retry once if the response is empty or still contains Korean.
            # A second pass with an explicit "English only" reminder often
            # rescues chunks the bare bulk-simple prompt missed — leaving
            # the source Korean visible would otherwise look like a bug.
            if not translated or _contains_hangul(translated):
                problem = (
                    "empty/placeholder response"
                    if not translated else "response still contained Korean characters"
                )
                if verbose:
                    print(
                        f"  translate_body chunk {chunk.id}: retry ({problem})"
                    )
                retry_messages = messages + [{
                    "role": "user",
                    "content": (
                        "Your previous response was unusable. "
                        f"Problem: {problem}. "
                        "Translate the Korean text above into English. "
                        "Output English only — no Korean characters anywhere, "
                        "no markdown, no commentary. "
                        "Keep every [EQUATION_N] marker verbatim and in the "
                        "same relative position."
                    ),
                }]
                raw = client.complete(retry_messages)
                translation_text, new_terms = parse_body_simple_response(raw)
                text = clean_translation_text(translation_text)
                translated = _apply_paragraph_id_prefix(chunk, text) if text else ""

            if translated and not _contains_hangul(translated):
                chunk.translation = translated
                added = 0
                for ko, en in new_terms.items():
                    if ko not in glossary:
                        glossary[ko] = en
                        added += 1
                if verbose and added:
                    print(
                        f"  translate_body chunk {chunk.id}: "
                        f"glossary extended with {added} new term(s)"
                    )
            elif verbose:
                print(
                    f"  translate_body chunk {chunk.id}: "
                    "still empty/Korean after retry"
                )
        except Exception as exc:
            chunk.translation = ""
            if verbose:
                print(
                    f"  translate_body chunk {chunk.id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        if not chunk.translation:
            failed.append(chunk.id)

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
