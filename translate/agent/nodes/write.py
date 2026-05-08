"""write — apply translated chunks back to docx paragraphs and save."""

from __future__ import annotations

import re
import time
import traceback


_HANGUL_RE = re.compile(r"[가-힯]")

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
#
# A "symbol" is any non-whitespace, non-{,;} run that starts with either
# a letter (Latin / Greek / math-italic) or one of these math-bracket chars:
#   〖 〗 ( ) [ ] | { }
# This is broad enough to catch '〖BIT〗_3k', '〖BIT〗_(3k+1)', 'θ_k', 'LLR',
# '|μ|' etc. while still rejecting common English words at clause boundaries.
_SYM_FIRST = r"[^\W\d_]|[〖〗()\[\]|{}]"
_PARAM_CLAUSE_START_RE = re.compile(
    r"(?:^|[;,]\s*|\s+and\s+|\s+wherein\s+|\s+where\s+|\s+in\s+which\s+)"
    rf"(?P<sym>(?:{_SYM_FIRST})[^\s,;]*)"
    r"\s+(?:is|are|denotes?|represents?|stands?\s+for|indicates?|means?)\b",
    re.IGNORECASE | re.UNICODE,
)


_VERB_RE = (
    r"is|are|denotes?|represents?|stands?\s+for|indicates?|means?"
)
# Matches the broken 'sym1 sym2 ... symN  <verb>  desc1; desc2; ...' shape:
#   'LLR θ_k 〖BIT〗_3k μ is the bit reliability data; is the phase-difference …'
_LIST_THEN_DESCS_RE = re.compile(
    rf"^\s*(?P<sym_list>(?:(?:{_SYM_FIRST})[^\s,;]*)"
    rf"(?:[\s,]+(?:(?:{_SYM_FIRST})[^\s,;]*)){{1,15}})"
    rf"\s+(?P<verb>{_VERB_RE})\s+"
    r"(?P<descs>.+)\Z",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)


def _split_into_parameter_clauses(text: str) -> list[tuple[str, str]]:
    """Parse a description blob into (symbol, full_clause) pairs.

    Try the degenerate 'symbols listed, then bare descriptions' shape FIRST:
        'LLR θ_k 〖BIT〗_3k μ is X; is Y; is Z'
    so we don't get fooled by an inline 'k is an integer' fragment that pass 1
    would otherwise return as the only match. If that shape doesn't fit, fall
    back to the well-formed '<sym> <verb> <desc>' scan.

    Each returned clause includes its symbol so it can be inserted back into
    the document as-is.
    """
    cleaned = re.sub(
        r"^[\s,;]*(?:where|wherein|in\s+which)\s+",
        "", text.strip(), flags=re.IGNORECASE,
    )

    # Pass 2 (try first): degenerate 'list-then-descriptions' shape.
    repaired = _repair_list_then_descs(cleaned)
    if repaired:
        return repaired

    # Pass 1: well-formed clauses.
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


_INNER_CLAUSE_RE = re.compile(
    rf"^\s*(?P<sym>(?:{_SYM_FIRST})[^\s,;]*)\s+(?:{_VERB_RE})\b",
    re.IGNORECASE | re.UNICODE,
)


def _repair_list_then_descs(blob: str) -> list[tuple[str, str]]:
    """Detect and pair the broken format
        'sym1 sym2 ... symN  <verb>  desc1; desc2; ...; descN'

    Pairing strategy is greedy by *next unused symbol*, not by index, so that
    if one of the description fragments is already a complete clause about a
    differently-named symbol (e.g. an inline 'k is an integer'), we don't
    overwrite an unused symbol slot — the inline fragment uses its own symbol
    and the unused symbol gets a placeholder. Nothing is silently dropped.

    Returns [] when the shape doesn't match.
    """
    m = _LIST_THEN_DESCS_RE.match(blob)
    if not m:
        return []

    sym_list_raw = m.group("sym_list")
    descs_raw = m.group("descs")

    symbols = [s.strip(" ,;.") for s in re.split(r"[\s,]+", sym_list_raw) if s.strip()]
    fillers = {"the", "a", "an", "of", "in", "at", "on", "and", "or", "to", "for"}
    if any(s.lower() in fillers for s in symbols):
        return []
    if len(symbols) < 2:
        return []

    desc_fragments = [
        d.strip(" ,;.\t\n") for d in re.split(r"\s*;\s*", descs_raw) if d.strip()
    ]

    pairs: list[tuple[str, str]] = []
    used: set[str] = set()
    sym_iter = iter(symbols)

    def _next_unused() -> str | None:
        for s in sym_iter:
            if s.lower() not in used:
                return s
        return None

    for frag in desc_fragments:
        inner = _INNER_CLAUSE_RE.match(frag)
        if inner:
            # Frag is already a complete '<sym> <verb> <desc>' clause —
            # use its own symbol and don't burn one from the list.
            inner_sym = inner.group("sym")
            pairs.append((inner_sym, frag))
            used.add(inner_sym.lower())
            continue
        sym = _next_unused()
        if sym is None:
            # Nothing left to pair against — append as a continuation of the
            # previous clause so the description text isn't lost.
            if pairs:
                last_sym, last_clause = pairs[-1]
                pairs[-1] = (last_sym, f"{last_clause}; {frag}")
            continue
        cleaned_frag = re.sub(
            r"^(?:is|are|denotes?|represents?|stands?\s+for|indicates?|means?)\s+",
            "", frag, flags=re.IGNORECASE,
        ).strip()
        pairs.append((sym, f"{sym} is {cleaned_frag}"))
        used.add(sym.lower())

    # Any symbols that never received a description: emit a placeholder so
    # they remain visible in the output (and you can tell they need attention).
    for sym in symbols:
        if sym.lower() not in used:
            pairs.append((sym, f"{sym} is …"))

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


def _vars_are_disjoint(eq_vars: list[set[str]]) -> bool:
    """True when every pair of equations has zero variables in common.

    Disjoint case (A=B+C, X=Y×Z, M=N-P) — splitting clauses per equation
    is meaningful. Shared case (3 LLR equations all using LLR/BIT/θ/k/μ) —
    splitting is meaningless; the legend describes them collectively, so
    we just clean up the format and keep it as a single block.
    """
    if len(eq_vars) < 2:
        return False
    for i in range(len(eq_vars)):
        for j in range(i + 1, len(eq_vars)):
            if eq_vars[i] & eq_vars[j]:
                return False
    return True


def _candidate_match_keys(sym: str) -> set[str]:
    """All keys against which we'll try to match this clause's symbol.

    Includes the full normalized form AND each Unicode letter run in it,
    so 'θ_k' → {'θ_k', 'θ', 'k'} and '〖BIT〗_3k' → {'〖bit〗_3k', 'bit', 'k'}.
    Equation extraction yields letter runs only ({'bit', 'k', …}), so the
    letter-run keys are what actually drive the match.
    """
    norm = normalize_symbol(sym).lower()
    runs = set(re.findall(r"[^\W\d_]+", norm, re.UNICODE))
    runs.add(norm)
    return runs


def _format_legend_clauses(pairs: list[tuple[str, str]]) -> str:
    """Join clauses into a per-parameter legend body."""
    return "; ".join(c for _, c in pairs)


def _split_per_equation(
    parts: list[str],
    eq_vars: list[set[str]],
    pairs: list[tuple[str, str]],
    blob_idx: int,
) -> list[str]:
    """Distribute clauses across equations using letter-run matching."""
    groups: list[list[str]] = [[] for _ in eq_vars]
    unmatched: list[str] = []
    for sym, clause in pairs:
        keys = _candidate_match_keys(sym)
        assigned = False
        for i, vars_ in enumerate(eq_vars):
            if keys & vars_:
                groups[i].append(clause)
                assigned = True
                break
        if not assigned:
            unmatched.append(clause)
    if not any(groups):
        return parts

    new_body: list[str] = []
    for i, group in enumerate(groups):
        if not group:
            new_body.append("")
            continue
        joined = "; ".join(group)
        end = "." if i == len(groups) - 1 else ";"
        new_body.append(", where " + joined + end)

    if unmatched:
        tail = "; ".join(unmatched)
        target = blob_idx if 0 <= blob_idx < len(new_body) else len(new_body) - 1
        if new_body[target]:
            stripped = new_body[target].rstrip(".;")
            end = "." if target == len(new_body) - 1 else ";"
            new_body[target] = stripped + "; " + tail + end
        else:
            end = "." if target == len(new_body) - 1 else ";"
            new_body[target] = ", where " + tail + end

    return [parts[0]] + new_body


def _reformat_only(
    parts: list[str],
    pairs: list[tuple[str, str]],
    blob_idx: int,
) -> list[str]:
    """Convert the blob into per-clause text and keep it at its original slot.

    Used when the equations share variables (legend is collective) or when
    we couldn't extract any equation variables to match against. The
    important property is that ALL clauses are preserved AND each parameter
    gets its own clause — no list-then-descs, no respectively, no dropped
    parameters.
    """
    is_last = blob_idx == len(parts) - 2
    end = "." if is_last else ";"
    text = "wherein " + _format_legend_clauses(pairs) + end
    new_body = list(parts[1:])
    new_body[blob_idx] = text
    return [parts[0]] + new_body


def _redistribute_grouped_parameters(
    parts: list[str],
    equation_indices: list[int],
    records,
) -> list[str]:
    """Rewrite a grouped parameter blob into per-clause form.

    Three behaviors:
      1. Already distributed (multiple body slots non-empty) → return unchanged.
      2. Shared-variable equations or no extractable variables →
         reformat the blob into '<sym> is <desc>; <sym> is <desc>; …' and
         keep it at the slot the LLM put it.
      3. Disjoint-variable equations (e.g. A=B+C, X=Y×Z, M=N-P) → split per
         equation by letter-run matching, so each equation is followed by
         only its own parameter clauses.

    Never drops a clause. Inline complete clauses (e.g. 'k is an integer'
    sitting in a fragment) are kept verbatim and use their own symbol so
    they don't burn an unused symbol slot.
    """
    if len(parts) <= 1 or not equation_indices:
        return parts

    body = parts[1:]
    non_empty = [(i, p.strip()) for i, p in enumerate(body) if p.strip()]
    if len(non_empty) != 1:
        return parts
    blob_idx, blob = non_empty[0]

    pairs = _split_into_parameter_clauses(blob)
    if len(pairs) < 2:
        return parts

    eq_vars = _equation_variable_sets(equation_indices, records)

    if any(eq_vars) and _vars_are_disjoint(eq_vars):
        return _split_per_equation(parts, eq_vars, pairs, blob_idx)
    return _reformat_only(parts, pairs, blob_idx)


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


def _safe_apply_chunk(chunk: Chunk, records, font: str, progress, verbose: bool) -> bool:
    """Apply one chunk's translation, isolating any crash to that chunk only.

    Returns True on success. On failure: logs the chunk id + traceback, leaves
    the chunk's source paragraphs untouched (Korean visible), and returns False
    so the caller can report how many chunks failed.

    Why per-chunk isolation: a single paragraph with unusual XML (rare drawing
    constructs, custom field codes, fractured runs, etc.) used to abort the
    entire write. The user then got back the *pre-copied original* — a silent
    Korean failure. Now any one failure leaves only that chunk's source visible.
    """
    try:
        _apply_chunk(chunk, records, font)
        return True
    except Exception as exc:
        msg = f"  WRITE FAILED on chunk {chunk.id} (paragraphs {chunk.paragraph_indices}): {type(exc).__name__}: {exc}"
        progress(msg)
        if verbose:
            traceback.print_exc()
        return False


def _korean_char_ratio(doc) -> float:
    """Fraction of letters in the document that are Hangul.

    Char-based instead of paragraph-based because static section headers like
    'CLAIMS' / 'ABSTRACT' get rewritten by ``apply_static`` even when every
    LLM call fails — those English headers would otherwise dilute a
    paragraph-based ratio and hide the catastrophe. Counts only letters
    (skips digits, punctuation, whitespace) so paragraph numbering and
    bracket decoration don't skew the ratio either.
    """
    hangul = 0
    letters = 0
    for p in doc.paragraphs:
        for ch in (p.text or ""):
            if ch.isalpha():
                letters += 1
                if "가" <= ch <= "힣":
                    hangul += 1
    return hangul / letters if letters else 0.0


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

    # Apply body, abstract, claims chunks. Per-chunk isolation so one bad
    # paragraph doesn't sink the whole document save.
    failed = 0
    total = 0
    for chunk in state.get("chunks_body", []):
        total += 1
        if not _safe_apply_chunk(chunk, indexed_records, font, progress, verbose):
            failed += 1
    for chunk in state.get("chunks_abstract", []):
        total += 1
        if not _safe_apply_chunk(chunk, indexed_records, font, progress, verbose):
            failed += 1
    for chunk in state.get("chunks_claims", []):
        total += 1
        if not _safe_apply_chunk(chunk, indexed_records, font, progress, verbose):
            failed += 1

    if total and failed:
        progress(f"WARNING: {failed}/{total} chunks failed to write — those paragraphs remain in Korean.")

    # Abstract word count footer — insert after the LAST paragraph of the abstract
    # chunk (so the footer appears after the translated body, not in the middle).
    abstract_chunks = state.get("chunks_abstract", [])
    if abstract_chunks and abstract_chunks[0].translation:
        try:
            ab = abstract_chunks[0]
            last_idx = ab.paragraph_indices[-1]
            last_para = indexed_records[last_idx].para
            count = word_count(ab.translation)
            insert_para_after(last_para, f"({count})", font)
            if verbose:
                print(f"\nABSTRACT word count → ({count})")
        except Exception as exc:
            progress(f"  WARNING: abstract word-count footer failed: {type(exc).__name__}: {exc}")

    # Sanity check: refuse to save an output that is overwhelmingly Korean.
    # That signals every chunk's LLM call returned untranslated text (rate
    # limit, auth error, network), or the entire write loop bailed out.
    # Char-based ratio so static English section headers can't mask a
    # catastrophic translation failure.
    ratio = _korean_char_ratio(doc)
    if ratio > 0.40:
        raise RuntimeError(
            f"Translation appears to have failed: {ratio:.0%} of the document's letters are still Hangul. "
            f"Refusing to save {output_path} so you don't end up with a fake-translated file. "
            "Check the LLM connection (LLM_API_KEY, LLM_BASE_URL) and rerun."
        )

    doc.save(output_path)
    elapsed = time.time() - started_at
    minutes, seconds = divmod(int(elapsed), 60)
    elapsed_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
    progress(f"Done in {elapsed_str} → {output_path}")
    if verbose:
        print(f"\nSaved → {output_path}")
        print(f"Total time: {elapsed_str}")

    return {}
