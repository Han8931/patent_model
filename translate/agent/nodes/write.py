"""write — apply translated chunks back to docx paragraphs and save."""

from __future__ import annotations

import re
import time

from ..docx_utils import (
    consolidate_formula_math_into,
    equations_in_paragraph,
    extract_equation_variables,
    has_drawing,
    has_math,
    has_non_text_content,
    insert_para_after,
    normalize_symbol,
    replace_text,
    word_count,
)
from ..state import Chunk, TranslationState


_EQUATION_TOKEN_RE = re.compile(r'\[EQUATION(?:_\d+)?\]')

# Splits an English description blob like
#   'where A is a thickness, B is a width, X is a length'
# into per-symbol clauses. The verb may be 'is/are/denotes/represents/...'.
_PARAM_CLAUSE_START_RE = re.compile(
    r"(?:^|[;,]\s*|\s+and\s+)"
    r"(?P<sym>[^\W\d_][^\W\d_0-9_]*[\w₀-₉_']*)"
    r"\s+(?:is|are|denotes?|represents?|stands?\s+for|indicates?|means?)\b",
    re.IGNORECASE | re.UNICODE,
)


def _split_into_parameter_clauses(text: str) -> list[tuple[str, str]]:
    """Parse a description blob into (symbol, full_clause) pairs.

    Drops a leading 'where'/'wherein'/'in which' if present. Each returned
    clause includes the symbol so it can be inserted back into the document
    as-is. Preserves source order.
    """
    cleaned = re.sub(
        r"^[\s,;]*(?:where|wherein|in\s+which)\s+",
        "", text.strip(), flags=re.IGNORECASE,
    )
    matches = list(_PARAM_CLAUSE_START_RE.finditer(cleaned))
    if not matches:
        return []
    pairs: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        sym = m.group("sym")
        clause_start = m.start("sym")
        clause_end = matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)
        clause = cleaned[clause_start:clause_end].strip(" ,;.\t\n")
        pairs.append((sym, clause))
    return pairs


def _equation_variable_sets(equation_indices: list[int], records) -> list[set[str]]:
    """For each equation paragraph, the set of (normalized, lowercased) variable names."""
    sets: list[set[str]] = []
    for eq_idx in equation_indices:
        para = records[eq_idx].para
        names: set[str] = set()
        for omath in equations_in_paragraph(para):
            for v in extract_equation_variables(omath):
                names.add(normalize_symbol(v).lower())
        sets.append(names)
    return sets


def _redistribute_grouped_parameters(
    parts: list[str],
    equation_indices: list[int],
    records,
) -> list[str]:
    """If the LLM bunched all parameter clauses at one slot, reassign each
    clause to the equation that actually uses its symbol.

    Bails out (returns ``parts`` unchanged) when:
      - parts is already distributed across multiple slots,
      - there's no recognizable per-symbol structure in the blob,
      - none of the clause symbols match any equation's variables.

    Never drops a clause: unmatched clauses go to the slot that originally
    held the blob, so the legend text always survives the rewrite.
    """
    if len(parts) <= 1 or not equation_indices:
        return parts

    body = parts[1:]
    non_empty = [(i, p.strip()) for i, p in enumerate(body) if p.strip()]
    if len(non_empty) != 1:
        return parts  # already split, or empty — nothing to do
    blob_idx, blob = non_empty[0]

    pairs = _split_into_parameter_clauses(blob)
    if len(pairs) < 2:
        return parts

    eq_vars = _equation_variable_sets(equation_indices, records)
    if not any(eq_vars):
        return parts

    groups: list[list[str]] = [[] for _ in equation_indices]
    unmatched: list[str] = []
    for sym, clause in pairs:
        norm = normalize_symbol(sym).lower()
        assigned = False
        for i, vars_ in enumerate(eq_vars):
            if norm in vars_:
                groups[i].append(clause)
                assigned = True
                break
        if not assigned:
            unmatched.append(clause)

    if not any(groups):
        return parts  # no symbol matched any equation — give up

    # Format each group into "<leading punct>where <c1>; <c2>; ..." form.
    new_body: list[str] = []
    for i, group in enumerate(groups):
        if not group:
            new_body.append("")
            continue
        joined = "; ".join(group)
        # Last group ends with '.', earlier ones with ';' to chain into next eq.
        if i == len(groups) - 1:
            joined = ", where " + joined + "."
        else:
            joined = ", where " + joined + ";"
        new_body.append(joined)

    # Append unmatched clauses to the slot the blob originally lived in,
    # so we never silently drop legend text.
    if unmatched:
        tail = "; ".join(unmatched)
        target = blob_idx if 0 <= blob_idx < len(new_body) else len(new_body) - 1
        if new_body[target]:
            # Splice into existing group.
            stripped = new_body[target].rstrip(".;")
            new_body[target] = stripped + "; " + tail + ("." if target == len(new_body) - 1 else ";")
        else:
            new_body[target] = ", where " + tail + ("." if target == len(new_body) - 1 else ";")

    return [parts[0]] + new_body


def _claim_equation_indices(chunk: Chunk, records) -> list[int]:
    return [
        idx for idx in chunk.paragraph_indices[1:]
        if has_math(records[idx].para) and not has_drawing(records[idx].para)
    ]


def _immediate_next_text_paragraph_index(
    chunk: Chunk,
    records,
    after_idx: int,
    used: set[int],
    equation_indices: set[int],
) -> int | None:
    """Return the chunk paragraph that comes IMMEDIATELY after ``after_idx``,
    only if it is an unused text paragraph.

    Why "immediately": for a claim shaped like
        [head] : [eq1] [eq2] [eq3] [legend]
    the legend is the first text paragraph after eq1, but it really belongs
    to eqN — sending desc1 to it would land far away from eq1. By restricting
    to the chunk's *next* index, we send descriptions to a sibling text
    paragraph only when one is actually adjacent (e.g. eq1 then legend1 in a
    paired layout); otherwise we leave the slot empty so the caller can
    insert_para_after eq_idx and place the description right next to its
    equation.
    """
    indices = chunk.paragraph_indices
    try:
        pos = indices.index(after_idx)
    except ValueError:
        return None
    if pos + 1 >= len(indices):
        return None
    nxt = indices[pos + 1]
    if nxt in used or nxt in equation_indices:
        return None
    if has_non_text_content(records[nxt].para):
        return None
    return nxt


def _legend_reference_para(chunk: Chunk, records, equation_set: set[int]):
    """Return the paragraph object that should serve as a formatting template
    for inserted description paragraphs.

    Heuristic: the LAST non-equation, non-image text paragraph in the chunk
    after the equations — i.e. the original "여기서, …" legend paragraph.
    Inserted descriptions inherit its <w:pPr> so they sit alongside centered
    equations without inheriting the equations' centered alignment.
    """
    for idx in reversed(chunk.paragraph_indices[1:]):
        if idx in equation_set:
            continue
        if has_non_text_content(records[idx].para):
            continue
        return records[idx].para
    return None


def _apply_claim_with_equations(chunk: Chunk, records, font: str) -> bool:
    """Apply a translated claim while preserving equation paragraph alignment."""
    equation_indices = _claim_equation_indices(chunk, records)
    if not equation_indices:
        return False

    translation = chunk.translation or ""
    head_idx = chunk.paragraph_indices[0]
    used_text_indices = {head_idx}

    if _EQUATION_TOKEN_RE.search(translation):
        parts = _EQUATION_TOKEN_RE.split(translation, maxsplit=len(equation_indices))
        while len(parts) < len(equation_indices) + 1:
            parts.append("")
    else:
        # If the LLM drops the marker, keep the equations in place and put the
        # translated claim text before them rather than forcing inline layout.
        parts = [translation] + [""] * len(equation_indices)

    # If the LLM grouped every parameter clause at one slot (typical when the
    # Korean source has 'eq1 eq2 eq3 + combined legend'), redistribute by
    # matching each clause's symbol to the equation that actually uses it.
    parts = _redistribute_grouped_parameters(parts, equation_indices, records)

    replace_text(records[head_idx].para, parts[0].strip(), font)

    for eq_idx in equation_indices:
        # Clear any surrounding Korean text but leave the equation XML and its
        # original paragraph formatting/alignment untouched.
        replace_text(records[eq_idx].para, "", font)

    equation_set = set(equation_indices)
    legend_format = _legend_reference_para(chunk, records, equation_set)

    for pos, eq_idx in enumerate(equation_indices, start=1):
        segment = _EQUATION_TOKEN_RE.sub("", parts[pos]).strip()
        if not segment:
            continue

        target_idx = _immediate_next_text_paragraph_index(
            chunk, records, eq_idx, used_text_indices, equation_set
        )
        if target_idx is None:
            insert_para_after(
                records[eq_idx].para, segment, font,
                format_ref_para=legend_format,
            )
            continue

        replace_text(records[target_idx].para, segment, font)
        used_text_indices.add(target_idx)

    for idx in chunk.paragraph_indices[1:]:
        if idx in equation_set or idx in used_text_indices:
            continue
        replace_text(records[idx].para, "", font)

    return True


def _apply_chunk(chunk: Chunk, records, font: str) -> None:
    """Write the chunk's translation into the FIRST paragraph; blank the rest.

    Claims with standalone equation paragraphs are handled separately so Word's
    original equation paragraph alignment is preserved. For other multi-paragraph
    chunks, formula equations from trailing paragraphs are moved into the head
    paragraph so replace_text() can interleave text around [EQUATION] markers.

    If the translation is empty/missing, leave the original paragraph untouched
    so the source text remains visible as a flag.
    """
    if not chunk.paragraph_indices:
        return
    if not chunk.translation or not chunk.translation.strip():
        return  # leave Korean visible — better than silent disappearance

    if chunk.kind == "claim" and _apply_claim_with_equations(chunk, records, font):
        return

    head_idx = chunk.paragraph_indices[0]
    head_record = records[head_idx]
    trailing = [records[idx].para for idx in chunk.paragraph_indices[1:]]

    # Pull formula equations out of trailing paragraphs and append to the head
    # so [EQUATION] placeholders in the translation can be interleaved with
    # actual <m:oMath> elements that now live in the head paragraph.
    if trailing:
        consolidate_formula_math_into(head_record.para, trailing)

    replace_text(head_record.para, chunk.translation, font)

    # Blank the trailing paragraphs. replace_text only modifies <w:r> text runs;
    # any leftover XML (drawings, Korean math being removed) is handled inside
    # replace_text. After consolidation, formula equations are no longer here.
    for idx in chunk.paragraph_indices[1:]:
        replace_text(records[idx].para, "", font)


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
