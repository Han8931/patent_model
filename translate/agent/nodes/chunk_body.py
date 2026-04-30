"""chunk_body — greedy join: merge a paragraph with the next when it ends with ':' or ','.

Boundary rules:
  - A paragraph that contains an embedded equation/image (mixed=True) is a hard boundary.
    It becomes its own singleton chunk and is never absorbed into a neighbor; doing so
    would either blank the paragraph's text runs (losing the equation context) or strand
    the equation in the wrong location.
  - Index gaps (e.g. a pure-equation paragraph sitting between two text records) also
    break the merge — only consecutive paragraph indices are eligible to join.
"""

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

        # Mixed paragraphs (text + equation) are singleton chunks — never merge into them
        # or out of them.
        if not head.mixed:
            while i + len(group) < len(body_records):
                tail = group[-1]
                nxt = body_records[i + len(group)]
                # Stop at section boundary
                if nxt.section != head.section:
                    break
                # Stop on index gap (e.g. an image paragraph sat between)
                if nxt.index != tail.index + 1:
                    break
                # Stop if the next paragraph is mixed (it must stand alone)
                if nxt.mixed:
                    break
                # Continue only if the previous paragraph clearly didn't finish
                if not _is_continuation(tail.raw):
                    break
                group.append(nxt)

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
