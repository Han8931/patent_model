"""Per-paragraph in-place translator for equation-bearing chunks.

The chunk-level path joins multiple Word paragraphs into one chunk text, sends
that to the LLM, then redistributes the English back across the source
paragraphs in the write step. For equation-bearing chunks this redistribution
is the source of most "where did the equation end up" bugs — the LLM
rearranges markers, drops some, or glues IDs together, and the write step
can't always recover.

This module bypasses both the chunk-level merge and the redistribute step by
translating EACH PARAGRAPH INDEPENDENTLY:

  * Pure-math paragraphs (kind == "image") are SKIPPED entirely — Word's
    OMML XML never gets touched, so the equation stays at its exact source
    position by construction.
  * Text and mixed (text + inline math) paragraphs go through their own
    focused LLM call. Inline math elements stay at their original XML
    position thanks to ``_replace_text_with_math_placeholders`` inside
    ``replace_text``.
  * The leading ``[NNN]`` paragraph-ID prefix is stripped before the LLM
    call and reattached afterwards, exactly like the chunk-level path does.

The caller sets ``chunk.applied_in_place = True`` after this runs so the
write node skips the chunk and doesn't re-apply anything.
"""

from __future__ import annotations

import re
from typing import Callable

from ..agent.docx_utils import (
    extract_all_text,
    extract_math_texts,
    postprocess,
    replace_text,
)
from ..agent.glossary import clean_translation_text, extract_json_block, merge_terms
from .nodes.translate_claims import _strip_markdown
from .sentence_translator import (
    _HANGUL_RE,
    _LEGEND_HEADER_RE,
    needs_per_segment_translation,
    translate_chunk_by_sentence,
    translate_text_segment,
)


# Paragraph-ID prefix detector — same shape as in chunk_body.py. Repeated here
# to keep the module self-contained (lets us tolerate the leading whitespace
# that occasionally hides a marker on a w:br-split paragraph).
_PARAGRAPH_ID_RE = re.compile(r"^[\s ]*\[(\d{1,5})\][\s ]*", re.UNICODE)
_EQUATION_TOKEN_RE = re.compile(r"\[EQUATION(?:_\d+)?\]")


def _strip_paragraph_id(text: str) -> tuple[str, str | None]:
    m = _PARAGRAPH_ID_RE.match(text)
    if not m:
        return text, None
    return text[m.end():], f"[{m.group(1)}]"


def _number_equation_placeholders(text: str, start_n: int = 1) -> str:
    """Replace each bare '[EQUATION]' with '[EQUATION_1]', '[EQUATION_2]', …
    so the sentence-level translator can disambiguate when there are multiple
    inline math elements in one paragraph."""
    counter = [start_n - 1]

    def repl(_: re.Match) -> str:
        counter[0] += 1
        return f"[EQUATION_{counter[0]}]"

    # Use the bare-placeholder regex to avoid double-numbering an existing
    # '[EQUATION_3]' (which already has a number).
    return re.sub(r"\[EQUATION\](?!_)", repl, text)


def _replace_inline_symbols_with_markers(text: str, record) -> str:
    """Replace visible inline Word-math symbols in translated text with markers.

    Parameter legend translation needs to see symbols like ``α² + 1/2`` so the
    symbol-description pairing stays intact. ``replace_text`` still needs
    ``[EQUATION_N]`` anchors to keep the actual Word math XML in place. This
    bridge swaps the first translated occurrence of each source math symbol
    back to its corresponding marker just before writing.
    """
    out = text
    for idx, symbol in enumerate(extract_math_texts(record.para), 1):
        marker = f"[EQUATION_{idx}]"
        if marker in out:
            continue
        if symbol:
            out = out.replace(symbol, marker, 1)
    return out


def _simple_translate(
    client,
    korean_text: str,
    glossary: dict,
    build_segment_messages: Callable[[str, dict], list[dict]],
) -> tuple[str, dict]:
    """One focused LLM call for a paragraph with no [EQUATION_N] markers."""
    try:
        raw = client.complete(build_segment_messages(korean_text, glossary))
    except Exception:
        return "", {}
    data = extract_json_block(raw) or {}
    text = clean_translation_text(data.get("text")) or clean_translation_text(raw)
    return text, data if isinstance(data, dict) else {}


def translate_paragraph_in_place(
    record,
    *,
    client,
    glossary: dict,
    font_name: str,
    build_segment_messages: Callable[[str, dict], list[dict]],
    build_clause_messages: Callable[[str, str, dict], list[dict]],
    verbose: bool = False,
) -> bool:
    """Translate ONE paragraph and write the English back into its own XML.

    Returns True when an LLM call was made and the paragraph was modified
    (caller may use this for progress / cost accounting); False when the
    paragraph was skipped (pure math, blank, or no extractable Korean).

    Inline ``<m:oMath>`` elements stay at their source XML position. The
    function delegates to ``replace_text``, which uses
    ``_replace_text_with_math_placeholders`` for inline math paragraphs and
    plain text replacement for everything else.
    """
    if record.kind == "image":
        return False
    para = record.para
    # Inline parameter legends must expose their symbols to the clause
    # translator; other mixed paragraphs use placeholders so the writer can
    # interleave text around math elements without moving them.
    raw_with_symbols = extract_all_text(para)
    use_symbol_legend = bool(getattr(record, "mixed", False)) and bool(
        _LEGEND_HEADER_RE.search(raw_with_symbols)
    )
    raw = (
        raw_with_symbols
        if use_symbol_legend
        else extract_all_text(para, math_as_placeholder=True)
    )
    if not raw.strip():
        return False

    # Strip any leading '[NNN]' paragraph ID and stash it for re-injection.
    stripped, id_prefix = _strip_paragraph_id(raw)

    # Renumber any bare '[EQUATION]' to '[EQUATION_1..N]' so the sentence
    # translator can split on stable, distinct markers.
    numbered = (
        stripped
        if use_symbol_legend
        else _number_equation_placeholders(stripped)
    )

    if needs_per_segment_translation(numbered):
        # Inline equations OR inline [NNN] markers — sentence-level path
        # translates each text segment in isolation and passes markers through
        # verbatim.
        en = translate_chunk_by_sentence(
            numbered, client, glossary,
            build_segment_messages=build_segment_messages,
            build_clause_messages=build_clause_messages,
        )
        data: dict = {}
    else:
        # No equation markers in this paragraph. Still route through
        # ``translate_text_segment`` (not a single simple call) because the
        # paragraph may be a parameter legend ('여기서, X는 …, Y는 …') —
        # the segment translator detects the legend header and translates
        # each clause separately, then joins with the standard ';' indent.
        en = translate_text_segment(
            client, numbered, glossary,
            build_segment_messages=build_segment_messages,
            build_clause_messages=build_clause_messages,
        )
        data = {}

    if not en:
        if verbose:
            print(f"  paragraph_translator: empty/placeholder for record {record.index}")
        return False

    # ``_llm_text`` already retries once on empty/Korean responses. If we
    # still see Hangul here, the segment/clause path couldn't translate
    # cleanly — leave the source paragraph untouched rather than overwrite
    # it with mixed Korean/English text that pretends to be a translation.
    if _HANGUL_RE.search(en):
        if verbose:
            print(
                f"  paragraph_translator: Korean still present after retry "
                f"for record {record.index}; skipping write"
            )
        return False

    # Reattach paragraph ID prefix at the very start.
    if id_prefix and not en.lstrip().startswith(id_prefix):
        en = f"{id_prefix} {en.lstrip()}"

    # The minimal segment / clause prompts sometimes return markdown
    # decorations (**bold**, leading bullets, '---' rules). Strip them
    # before postprocess so the docx output stays plain prose, matching
    # the chunk-level body path.
    en = _strip_markdown(en)
    en = postprocess(en)
    if use_symbol_legend:
        en = _replace_inline_symbols_with_markers(en, record)

    # Apply in-place. For a mixed paragraph (text + inline math),
    # ``replace_text`` routes through ``_replace_text_with_math_placeholders``
    # which inserts text segments BEFORE each <m:oMath> while leaving the
    # math elements at their source XML position.
    replace_text(para, en, font_name)

    if isinstance(data, dict):
        merge_terms(glossary, data.get("key_terms") or [])

    return True


def translate_chunk_per_paragraph(
    chunk,
    *,
    records,
    client,
    glossary: dict,
    font_name: str,
    build_segment_messages: Callable[[str, dict], list[dict]],
    build_clause_messages: Callable[[str, str, dict], list[dict]],
    verbose: bool = False,
) -> int:
    """Translate every paragraph in ``chunk.paragraph_indices`` independently.

    Returns the number of paragraphs actually translated (LLM calls made).
    Marks the chunk as ``applied_in_place=True`` so the write node skips it.
    """
    applied = 0
    for idx in chunk.paragraph_indices:
        if 0 <= idx < len(records):
            record = records[idx]
        else:
            continue
        if record is None:
            continue
        ok = translate_paragraph_in_place(
            record,
            client=client,
            glossary=glossary,
            font_name=font_name,
            build_segment_messages=build_segment_messages,
            build_clause_messages=build_clause_messages,
            verbose=verbose,
        )
        if ok:
            applied += 1
    chunk.applied_in_place = True
    # Stamp a translation marker so legacy logic that checks chunk.translation
    # can tell the chunk was handled. Empty string would trigger Korean-fallback
    # behavior in some callers, so use a sentinel non-empty value.
    chunk.translation = "[APPLIED_IN_PLACE]"
    return applied


def chunk_has_math(chunk, records) -> bool:
    """True when any paragraph in the chunk has math (pure-eq or inline)."""
    for idx in chunk.paragraph_indices:
        if not (0 <= idx < len(records)):
            continue
        r = records[idx]
        if r is None:
            continue
        if r.kind == "image":
            return True
        if getattr(r, "mixed", False):
            return True
        # Defensive: check the raw text for [EQUATION] markers in case
        # chunk_body produced them.
        if _EQUATION_TOKEN_RE.search(r.raw or ""):
            return True
    return False


def chunk_has_legend(chunk, records) -> bool:
    """True when any paragraph in the chunk opens with a Korean parameter
    legend ('여기서, …' / '상기 …에서, …' / '다만, …').

    These chunks need per-paragraph routing even when they contain no math,
    because translate_text_segment is the helper that knows how to split a
    legend into per-symbol clauses.
    """
    for idx in chunk.paragraph_indices:
        if not (0 <= idx < len(records)):
            continue
        r = records[idx]
        if r is None:
            continue
        if _LEGEND_HEADER_RE.search(r.raw or ""):
            return True
    return False


def chunk_needs_per_paragraph(chunk, records) -> bool:
    """Router predicate used by translate_body. The per-paragraph path is
    chosen whenever a chunk has math (so equation positions are preserved by
    construction) OR whenever it has a parameter legend (so the per-symbol
    clauses are split into individual LLM calls)."""
    return chunk_has_math(chunk, records) or chunk_has_legend(chunk, records)
