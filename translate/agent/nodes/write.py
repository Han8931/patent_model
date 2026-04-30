"""write — apply translated chunks back to docx paragraphs and save."""

from __future__ import annotations

import time

from ..docx_utils import has_non_text_content, insert_para_after, replace_text, word_count
from ..state import Chunk, TranslationState


def _apply_chunk(chunk: Chunk, records, font: str) -> None:
    """Write the chunk's translation into the FIRST paragraph; blank the rest.

    If the translation is empty/missing, leave the original paragraph untouched
    (rather than blanking it) so the source text remains visible as a flag.
    """
    if not chunk.paragraph_indices:
        return
    if not chunk.translation or not chunk.translation.strip():
        return  # leave Korean visible — better than silent disappearance

    head_idx = chunk.paragraph_indices[0]
    head_record = records[head_idx]
    replace_text(head_record.para, chunk.translation, font)

    for idx in chunk.paragraph_indices[1:]:
        rec = records[idx]
        # Don't blank a paragraph that carries an equation or drawing — clearing its
        # text runs would strand the equation visually. Leave the original Korean
        # text in place so the equation keeps its surrounding context.
        if has_non_text_content(rec.para):
            continue
        replace_text(rec.para, "", font)


def write(state: TranslationState) -> dict:
    doc = state["doc"]
    records = state["records"]
    font = state["font"]
    output_path = state["output_path"]
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)
    started_at = state.get("started_at", time.time())

    # Map index → record for O(1) lookup
    by_index = {r.index: r for r in records}
    indexed_records = [None] * (max(by_index) + 1) if by_index else []
    for idx, r in by_index.items():
        indexed_records[idx] = r

    progress("Writing translations…")

    # Apply body, abstract, claims chunks
    for chunk in state.get("chunks_body", []):
        _apply_chunk(chunk, indexed_records, font)
    for chunk in state.get("chunks_abstract", []):
        _apply_chunk(chunk, indexed_records, font)
    for chunk in state.get("chunks_claims", []):
        _apply_chunk(chunk, indexed_records, font)

    # Abstract word count footer — insert after the LAST paragraph of the abstract
    # chunk (so the footer appears after the translated body, not in the middle).
    abstract_chunks = state.get("chunks_abstract", [])
    if abstract_chunks and abstract_chunks[0].translation:
        ab = abstract_chunks[0]
        last_idx = ab.paragraph_indices[-1]
        last_para = indexed_records[last_idx].para
        count = word_count(ab.translation)
        insert_para_after(last_para, f"({count})", font)
        if verbose:
            print(f"\nABSTRACT word count → ({count})")

    doc.save(output_path)
    elapsed = time.time() - started_at
    minutes, seconds = divmod(int(elapsed), 60)
    elapsed_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
    progress(f"Done in {elapsed_str} → {output_path}")
    if verbose:
        print(f"\nSaved → {output_path}")
        print(f"Total time: {elapsed_str}")

    return {}
