"""chunk_body — greedy join: merge a paragraph with the next when it ends with ':' or ','."""

from __future__ import annotations

from ..sections import BODY_SECTIONS
from ..state import Chunk, TranslationState


_CONTINUATION_TAILS = (":", ",")


def _is_continuation(text: str) -> bool:
    """True if this paragraph clearly does not finish a sentence."""
    s = text.strip()
    return bool(s) and s.endswith(_CONTINUATION_TAILS)


def chunk_body(state: TranslationState) -> dict:
    records = state["records"]
    chunks: list[Chunk] = []

    # Walk all text records that belong to body sections (or have no recognised section).
    body_records = [
        r for r in records
        if r.kind == "text"
        and (r.section in BODY_SECTIONS or r.section is None)
    ]

    i = 0
    chunk_idx = 0
    while i < len(body_records):
        head = body_records[i]
        group = [head]
        # Greedy: consume following records as long as the previous ends in : or ,
        # and stays inside the same section.
        while (
            _is_continuation(group[-1].raw)
            and i + len(group) < len(body_records)
            and body_records[i + len(group)].section == head.section
        ):
            group.append(body_records[i + len(group)])

        chunks.append(Chunk(
            id=f"body-{chunk_idx}",
            section=head.section or "BODY",
            kind="body",
            paragraph_indices=[r.index for r in group],
            text="\n".join(r.raw for r in group),
        ))
        chunk_idx += 1
        i += len(group)

    return {"chunks_body": chunks}
