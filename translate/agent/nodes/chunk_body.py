"""chunk_body — greedy join: merge a paragraph with the next when it ends with ':' or ','.

Boundary rules:
  - A paragraph that contains an embedded equation/image (mixed=True) is a hard boundary.
    It becomes its own singleton chunk and is never absorbed into a neighbor; doing so
    would either blank the paragraph's text runs (losing the equation context) or strand
    the equation in the wrong location.
  - Index gaps (e.g. a pure-equation paragraph sitting between two text records) also
    break the merge — only consecutive paragraph indices are eligible to join.
  - Hard SIZE CAP: stop merging once the cumulative chunk text exceeds
    ``_CHUNK_MAX_CHARS``. Some patents have every paragraph ending in ',' which
    would otherwise collapse the entire BODY into a single multi-thousand-char
    chunk that overflows the LLM's output token limit and silently falls back
    to Korean.

Each chunk's equation placeholders are renumbered to [EQUATION_1], [EQUATION_2], ...
so the LLM can preserve relative order and the write node can split the translation
back onto the original equation positions.
"""

from __future__ import annotations

import re

from ..docx_utils import extract_all_text, extract_math_texts, has_drawing, has_math
from ..sections import BODY_SECTIONS
from ..state import Chunk, TranslationState


_CONTINUATION_TAILS = (":", ",")
_EQUATION_TOKEN_RE = re.compile(r'\[EQUATION(?:_\d+)?\]')

# Paragraph-ID prefix: '[001]', '[12]', '[0016]', etc. at the start of a
# paragraph's text. 1–5 digits, optional whitespace after.
_PARAGRAPH_ID_RE = re.compile(r'^\[(\d{1,5})\]\s*')


def _strip_paragraph_id(text: str) -> tuple[str, str | None]:
    """If ``text`` starts with a '[NNN]' paragraph ID, return (rest, prefix).
    Otherwise return (text, None). The prefix returned keeps the original
    bracket form so it can be re-injected verbatim onto the translation."""
    m = _PARAGRAPH_ID_RE.match(text)
    if not m:
        return text, None
    return text[m.end():], f"[{m.group(1)}]"


def _has_paragraph_id(text: str) -> bool:
    return bool(_PARAGRAPH_ID_RE.match(text))

# Approximate per-chunk character budget. Korean output ≈ 1 char per token, so
# ~1800 chars leaves headroom for the LLM's English answer plus its system+user
# prompt overhead within a typical 4–8k token context window. Patent paragraphs
# are usually 100–500 chars, so this still allows 4–18 paragraphs per chunk in
# the common case while preventing pathological merges.
_CHUNK_MAX_CHARS = 1800


def _is_continuation(text: str) -> bool:
    """True if this paragraph clearly does not finish a sentence."""
    s = text.strip()
    return bool(s) and s.endswith(_CONTINUATION_TAILS)


def _number_equation_placeholders(
    text: str,
    next_number: int,
) -> tuple[str, int, list[str]]:
    """Replace each [EQUATION] (and stray numbered forms) with stable [EQUATION_N]."""
    markers: list[str] = []

    def repl(_: re.Match) -> str:
        nonlocal next_number
        marker = f"[EQUATION_{next_number}]"
        markers.append(marker)
        next_number += 1
        return marker

    return _EQUATION_TOKEN_RE.sub(repl, text), next_number, markers


def _add_equation_context(
    context: dict[str, str],
    markers: list[str],
    formulas: list[str],
) -> None:
    for marker, formula in zip(markers, formulas):
        context[marker] = formula


def _body_text_record(r) -> bool:
    return (
        r.kind == "text"
        and (r.section in BODY_SECTIONS or r.section is None)
    )


def _body_formula_record(r) -> bool:
    return (
        r.kind == "image"
        and (r.section in BODY_SECTIONS or r.section is None)
        and has_math(r.para)
        and not has_drawing(r.para)
    )


def _same_body_section(a, b) -> bool:
    return a.section == b.section


def _can_append_to_body_group(group, nxt) -> bool:
    tail = group[-1]
    if not _same_body_section(tail, nxt):
        return False
    if nxt.index != tail.index + 1:
        return False

    if _body_formula_record(nxt):
        return not tail.mixed

    if not _body_text_record(nxt):
        return False
    if _has_paragraph_id(nxt.raw):
        return False

    # Keep equation descriptions/legends attached to the equation paragraph,
    # even when the legend's symbols are inline Word math objects.
    if _body_formula_record(tail):
        return True
    if nxt.mixed:
        return False

    return _is_continuation(tail.raw)


def chunk_body(state: TranslationState) -> dict:
    records = state["records"]
    progress = state.get("progress") or (lambda _: None)
    chunks: list[Chunk] = []

    body_records = [
        r for r in records
        if _body_text_record(r) or _body_formula_record(r)
    ]

    i = 0
    chunk_idx = 0
    while i < len(body_records):
        head = body_records[i]
        if not _body_text_record(head):
            i += 1
            continue
        group = [head]
        cumulative_chars = len(head.raw)

        # Mixed paragraphs are singleton chunks. Numbered paragraph-ID
        # paragraphs are also singleton text chunks, but may still carry their
        # immediately following standalone equation/legend block.
        if not head.mixed:
            while i + len(group) < len(body_records):
                nxt = body_records[i + len(group)]
                if not _can_append_to_body_group(group, nxt):
                    break
                if cumulative_chars + len(nxt.raw) > _CHUNK_MAX_CHARS:
                    break
                group.append(nxt)
                cumulative_chars += len(nxt.raw)

        next_eq = 1
        numbered_parts: list[str] = []
        equation_context: dict[str, str] = {}
        head_id_prefix: str | None = None
        for pos, r in enumerate(group):
            stripped_raw, id_prefix = _strip_paragraph_id(r.raw)
            if pos == 0:
                # Only the head paragraph's ID is preserved deterministically.
                # Trailing paragraphs (rare — only when continuation merge fires)
                # have their text blanked by the write step anyway, so their
                # IDs would be lost regardless.
                head_id_prefix = id_prefix
                source_text = stripped_raw
            else:
                source_text = r.raw  # keep as-is so the LLM's per-line rendering still works
            if _body_formula_record(r):
                source_text = extract_all_text(r.para) or "[EQUATION]"
            numbered, next_eq, markers = _number_equation_placeholders(
                source_text, next_eq
            )
            if markers and has_math(r.para) and not has_drawing(r.para):
                _add_equation_context(
                    equation_context,
                    markers,
                    extract_math_texts(r.para),
                )
            numbered_parts.append(numbered)

        chunks.append(Chunk(
            id=f"body-{chunk_idx}",
            section=head.section or "BODY",
            kind="body",
            paragraph_indices=[r.index for r in group],
            text="\n".join(numbered_parts),
            equation_context=equation_context,
            paragraph_id_prefix=head_id_prefix,
        ))
        chunk_idx += 1
        i += len(group)

    if chunks:
        max_chars = max(len(c.text) for c in chunks)
        progress(
            f"BODY: {len(chunks)} chunk(s) from {len(body_records)} paragraphs "
            f"(largest: {max_chars} chars)"
        )
    return {"chunks_body": chunks}
