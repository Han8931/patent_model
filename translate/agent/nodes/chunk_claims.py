"""chunk_claims — group all paragraphs of a single claim into one chunk.

The chunk's first paragraph_index is the standalone claim_header paragraph (when
present). The translated "N. <claim>" text is written to that paragraph by the
write node; all other paragraphs in the chunk are blanked.
"""

from __future__ import annotations

from ..state import Chunk, TranslationState


def chunk_claims(state: TranslationState) -> dict:
    records = state["records"]
    chunks: list[Chunk] = []

    current_claim_num: int | None = None
    current_indices: list[int] = []
    current_text_parts: list[str] = []
    header_index: int | None = None

    def flush() -> None:
        nonlocal current_claim_num, current_indices, current_text_parts, header_index
        if current_claim_num is not None and (current_indices or header_index is not None):
            indices: list[int] = []
            if header_index is not None:
                indices.append(header_index)
            indices.extend(current_indices)
            chunks.append(Chunk(
                id=f"claim-{current_claim_num}",
                section="CLAIMS",
                kind="claim",
                paragraph_indices=indices,
                text="\n".join(current_text_parts),
                claim_num=current_claim_num,
            ))
        current_indices = []
        current_text_parts = []
        header_index = None

    in_claims = False
    for r in records:
        if r.kind == "section_header":
            if r.mapped == "CLAIMS":
                in_claims = True
            else:
                flush()
                current_claim_num = None
                in_claims = False
            continue

        if not in_claims and r.section != "CLAIMS":
            continue

        if r.kind == "claim_header":
            flush()
            current_claim_num = r.claim_num
            header_index = r.index
            in_claims = True
            continue

        if r.kind == "text" and r.section == "CLAIMS":
            if r.claim_num is not None and r.claim_num != current_claim_num:
                flush()
                current_claim_num = r.claim_num
                # No standalone header — this paragraph is the head of the claim
                header_index = None
            current_indices.append(r.index)
            current_text_parts.append(r.raw)

    flush()
    return {"chunks_claims": chunks}
