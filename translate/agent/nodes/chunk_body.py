"""chunk_body — greedy join: merge a paragraph with the next when it ends with ':' or ','.

Boundary rules:
  - A paragraph that contains an embedded equation/image (mixed=True) is a hard boundary.
    It becomes its own singleton chunk and is never absorbed into a neighbor; doing so
    would either blank the paragraph's text runs (losing the equation context) or strand
    the equation in the wrong location.
  - Index gaps (e.g. a pure-equation paragraph sitting between two text records) also
    break the merge — only consecutive paragraph indices are eligible to join.

Each chunk's equation placeholders are renumbered to [EQUATION_1], [EQUATION_2], ...
so the LLM can preserve relative order and the write node can split the translation
back onto the original equation positions.
"""

from __future__ import annotations

import re

from ..sections import BODY_SECTIONS
from ..state import Chunk, TranslationState


_CONTINUATION_TAILS = (":", ",")
_EQUATION_TOKEN_RE = re.compile(r'\[EQUATION(?:_\d+)?\]')


def _is_continuation(text: str) -> bool:
    """True if this paragraph clearly does not finish a sentence."""
    s = text.strip()
    return bool(s) and s.endswith(_CONTINUATION_TAILS)


def _number_equation_placeholders(text: str, next_number: int) -> tuple[str, int]:
    """Replace each [EQUATION] (and stray numbered forms) with stable [EQUATION_N]."""

    def repl(_: re.Match) -> str:
        nonlocal next_number
        marker = f"[EQUATION_{next_number}]"
        next_number += 1
        return marker

    return _EQUATION_TOKEN_RE.sub(repl, text), next_number


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

        next_eq = 1
        numbered_parts: list[str] = []
        for r in group:
            numbered, next_eq = _number_equation_placeholders(r.raw, next_eq)
            numbered_parts.append(numbered)

        chunks.append(Chunk(
            id=f"body-{chunk_idx}",
            section=head.section or "BODY",
            kind="body",
            paragraph_indices=[r.index for r in group],
            text="\n".join(numbered_parts),
        ))
        chunk_idx += 1
        i += len(group)

    return {"chunks_body": chunks}
