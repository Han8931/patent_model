"""chunk_claims — group all paragraphs of a single claim into one chunk.

The chunk's first paragraph_index is the standalone claim_header paragraph (when
present). The translated "N. <claim>" text is written to that paragraph by the
write node; all other paragraphs in the chunk are blanked.

Math-only equation paragraphs are classified as image records so they can be
preserved, but inside claims they still belong to the claim text. Include them
as [EQUATION] placeholders so the LLM sees the limitation and the write node can
move the actual Word equation XML into the translated claim paragraph.
"""

from __future__ import annotations

import re

from ..docx_utils import extract_all_text, has_drawing, has_math
from ..state import Chunk, TranslationState


_EQUATION_TOKEN_RE = re.compile(r'\[EQUATION(?:_\d+)?\]')


def _number_equation_placeholders(text: str, next_number: int) -> tuple[str, int]:
    """Give each equation in a claim a stable marker for order preservation."""

    def repl(_: re.Match) -> str:
        nonlocal next_number
        marker = f"[EQUATION_{next_number}]"
        next_number += 1
        return marker

    return _EQUATION_TOKEN_RE.sub(repl, text), next_number


def chunk_claims(state: TranslationState) -> dict:
    records = state["records"]
    chunks: list[Chunk] = []

    current_claim_num: int | None = None
    current_indices: list[int] = []
    current_text_parts: list[str] = []
    header_index: int | None = None
    next_equation_number = 1

    def flush() -> None:
        nonlocal current_claim_num, current_indices, current_text_parts
        nonlocal header_index, next_equation_number
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
        next_equation_number = 1

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
            text, next_equation_number = _number_equation_placeholders(
                r.raw, next_equation_number
            )
            current_indices.append(r.index)
            current_text_parts.append(text)
            continue

        if (
            r.kind == "image"
            and r.section == "CLAIMS"
            and current_claim_num is not None
            and has_math(r.para)
            and not has_drawing(r.para)
        ):
            text, next_equation_number = _number_equation_placeholders(
                extract_all_text(r.para) or "[EQUATION]", next_equation_number
            )
            current_indices.append(r.index)
            current_text_parts.append(text)

    flush()
    return {"chunks_claims": chunks}
