"""translate_body — translate each body chunk; merge new terms into glossary.

Maintains a rolling buffer of the last few translated chunks and prepends
them to each new chunk's prompt as ``PREVIOUSLY TRANSLATED CONTEXT``. The
model uses that for antecedent/pronoun consistency and parallel structure,
but doesn't include it in its output. Configurable via ``LLM_CONTEXT_BACK``
(default: 2 chunks).
"""

from __future__ import annotations

import os
import re
import time
from collections import deque

from ..docx_utils import postprocess
from ..glossary import clean_translation_text
from ..prompts import (
    build_body_messages,
    build_body_retry_messages,
    build_clause_messages,
    build_segment_messages,
    parse_translation_dual_shape,
)
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


def _context_back() -> int:
    """How many recently-translated chunks to send as trailing context."""
    try:
        n = int(os.getenv("LLM_CONTEXT_BACK", "2"))
        return max(0, n)
    except ValueError:
        return 2


def _contains_hangul(text: str | None) -> bool:
    return bool(text and _HANGUL_RE.search(text))


def _extract_body_text(raw: str) -> tuple[str, dict[str, str]]:
    """Parse the body LLM response into (English_text, new_glossary_terms).

    Delegates to ``parse_translation_dual_shape`` which accepts both the
    JSON envelope shape (heavily-JSON-trained models like gpt-oss return
    this even when the slim prompt doesn't ask for it) AND the plain-text
    + ``===== GLOSSARY =====`` trailer shape the slim prompt requests.
    """
    return parse_translation_dual_shape(raw)


def _dump_raw_for_diagnosis(chunk: Chunk, raw: str, problem: str, progress) -> None:
    """Log the raw LLM response and parser diagnosis when a chunk fails.

    Without this it's impossible to tell whether the model returned empty
    content, schema-echo placeholders, refusal text, or a parseable shape
    that ``clean_translation_text`` happens to reject. The dump is bounded
    so it doesn't flood the log on long responses.
    """
    raw = raw or ""
    head = raw[:600].replace("\n", " ⏎ ")
    progress(
        f"  translate_body chunk {chunk.id} DIAG: problem={problem!r} "
        f"raw_len={len(raw)} raw_head={head!r}"
    )


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
    n_back = _context_back()
    trailing: deque[str] = deque(maxlen=n_back) if n_back > 0 else deque(maxlen=0)
    progress(
        f"Translating BODY ({total} chunks, trailing_context={n_back})…"
    )
    failed: list[str] = []

    def _heartbeat(i: int, dt: float) -> None:
        """Print a heartbeat every 10 chunks (and at the last chunk)."""
        if i % 10 == 0 or i == total:
            progress(f"  BODY {i:>3}/{total}  done   {dt:6.1f}s")

    for i, chunk in enumerate(chunks, 1):
        # Decide which path this chunk takes BEFORE the LLM call so failure
        # lines (if they fire) can name which path produced them.
        if chunk_needs_per_paragraph(chunk, indexed_records):
            path = "per-paragraph"
        elif needs_per_segment_translation(chunk.text):
            path = "sentence-level"
        else:
            path = "chunk"
        t0 = time.monotonic()

        # Equation-bearing chunks (any paragraph in the chunk has inline math
        # or is a standalone <m:oMath> paragraph) take the per-paragraph
        # in-place path: each source paragraph is translated independently and
        # written back into its own <w:p>, with <m:oMath> elements left
        # untouched at their source XML position. The write node skips these
        # chunks because chunk.applied_in_place is set.
        if path == "per-paragraph":
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
                progress(
                    f"  BODY {i:>3}/{total}  FAIL   {type(exc).__name__}: {exc}"
                )
                failed.append(chunk.id)
            else:
                dt = time.monotonic() - t0
                if applied:
                    _heartbeat(i, dt)
                else:
                    failed.append(chunk.id)
                    progress(
                        f"  BODY {i:>3}/{total}  empty  {dt:6.1f}s  "
                        "applied=0 paragraph(s)"
                    )
            if delay > 0:
                time.sleep(delay)
            continue

        # Chunks containing opaque markers ([EQUATION_N] or inline [NNN]
        # paragraph IDs from <w:br> line breaks) take the sentence-level path:
        # one LLM call per text segment and per per-symbol legend clause. The
        # markers never go through a single combined translation, so they
        # can't drift out of position or get glued together at the start.
        # Pure-prose chunks keep the cheaper single call.
        if path == "sentence-level":
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
                progress(
                    f"  BODY {i:>3}/{total}  FAIL   {type(exc).__name__}: {exc}"
                )
            chunk.translation = ""
            dt = time.monotonic() - t0
            if not en:
                failed.append(chunk.id)
                progress(f"  BODY {i:>3}/{total}  empty  {dt:6.1f}s")
            else:
                translated = _apply_paragraph_id_prefix(chunk, en)
                # Zero-tolerance: even one stray Hangul fragment after the
                # per-segment retry inside _llm_text means this assembled
                # chunk is unfit for output. Mark it failed; the writer
                # leaves the source paragraph untouched and the strict
                # write-time guard aborts the save if it remains.
                if _contains_hangul(translated):
                    failed.append(chunk.id)
                    progress(
                        f"  BODY {i:>3}/{total}  KOREAN {dt:6.1f}s "
                        "(sentence-level path still contained Hangul)"
                    )
                else:
                    chunk.translation = translated
                    trailing.append(translated)
                    _heartbeat(i, dt)
                    progress(f"  BODY {i:>3}/{total}  done   {dt:6.1f}s")
            if delay > 0:
                time.sleep(delay)
            continue

        messages = build_body_messages(
            chunk.text,
            glossary,
            equation_context=chunk.equation_context,
            trailing_translated=list(trailing),
        )
        last_problem = ""
        last_raw = ""
        for attempt in range(2):
            try:
                raw = client.complete(messages)
                last_raw = raw or ""
                text, new_terms = _extract_body_text(raw)
                if not text:
                    last_problem = "The response did not contain usable English text."
                else:
                    translated = _apply_paragraph_id_prefix(chunk, text)
                    if _contains_hangul(translated):
                        last_problem = "The response still contains Korean/Hangul text."
                    else:
                        chunk.translation = translated
                        trailing.append(translated)
                        # Extend glossary with any new ko→en pairs the model
                        # produced in the '===== GLOSSARY =====' trailer.
                        # setdefault preserves earlier (claim-derived) terms.
                        for ko, en in new_terms.items():
                            glossary.setdefault(ko, en)
                        break
            except Exception as exc:
                last_problem = f"The model call failed: {type(exc).__name__}: {exc}"
                last_raw = ""

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
            # Both attempts failed without `break`: dump the last raw response
            # so the log shows exactly what the model returned and why the
            # parser rejected it. Without this the user sees only "empty
            # (after retry)" and can't diagnose further.
            chunk.translation = ""
            _dump_raw_for_diagnosis(chunk, last_raw, last_problem, progress)

        dt = time.monotonic() - t0
        if not chunk.translation:
            failed.append(chunk.id)
            progress(f"  BODY {i:>3}/{total}  empty  {dt:6.1f}s (after retry)")
        else:
            _heartbeat(i, dt)

        if delay > 0:
            time.sleep(delay)

    if failed:
        progress(
            "WARNING: BODY translation unavailable for chunk(s): "
            + ", ".join(failed)
            + ". Those source paragraphs will remain unchanged unless the final Korean-ratio check aborts the file."
        )
    return {"chunks_body": chunks, "glossary": glossary}
