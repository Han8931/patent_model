"""chunk_abstract — collect ALL abstract paragraphs into a single chunk."""

from __future__ import annotations

from ..state import Chunk, TranslationState


def chunk_abstract(state: TranslationState) -> dict:
    records = state["records"]

    abstract_text_records = [
        r for r in records
        if r.kind == "text" and r.section == "ABSTRACT"
    ]

    if not abstract_text_records:
        return {"chunks_abstract": []}

    chunk = Chunk(
        id="abstract-0",
        section="ABSTRACT",
        kind="abstract",
        paragraph_indices=[r.index for r in abstract_text_records],
        text="\n".join(r.raw for r in abstract_text_records),
    )
    return {"chunks_abstract": [chunk]}
