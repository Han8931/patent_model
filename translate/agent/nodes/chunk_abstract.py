"""chunk_abstract — collect ALL abstract paragraphs into a single chunk.

Drops the redundant '[요약]' sub-heading that often appears between '[요약서]' and
the actual abstract body — its corresponding docx paragraph is blanked here so it
disappears from the output.
"""

from __future__ import annotations

from ..docx_utils import replace_text
from ..state import Chunk, TranslationState


_REDUNDANT_HEADINGS = {"[요약]", "【요약】"}


def chunk_abstract(state: TranslationState) -> dict:
    records = state["records"]
    font = state["font"]

    abstract_text_records = [
        r for r in records
        if r.kind == "text" and r.section == "ABSTRACT"
    ]

    # Blank and skip redundant sub-headings ([요약] inside the [요약서] section).
    keep: list = []
    for r in abstract_text_records:
        if r.raw.strip() in _REDUNDANT_HEADINGS:
            replace_text(r.para, "", font)
            continue
        keep.append(r)

    if not keep:
        return {"chunks_abstract": []}

    chunk = Chunk(
        id="abstract-0",
        section="ABSTRACT",
        kind="abstract",
        paragraph_indices=[r.index for r in keep],
        text="\n".join(r.raw for r in keep),
    )
    return {"chunks_abstract": [chunk]}
