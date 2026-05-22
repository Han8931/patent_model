"""Sentence-level translator for equation-bearing body chunks.

Standard body chunks go through ``translate_body`` in one LLM call per chunk.
That works for prose, but for paragraphs with inline ``[EQUATION_N]`` markers
and ``여기서, …`` parameter legends the single call risks:
  * Grouping the [EQUATION_N] markers together so all descriptions land
    after the last marker.
  * Rendering the legend in ``α, β, γ are X, Y, Z respectively`` form.
  * Dropping symbols whose Unicode shape confuses the LLM (e.g. 〖BIT〗_3k).

This module sidesteps those by translating equation-bearing chunks at a
finer granularity:

  1. ``[EQUATION_N]`` markers split the chunk text into TEXT units alternating
     with EQUATION units.
  2. Inside each TEXT unit, look for a Korean parameter legend ('여기서, …').
     If present, split the legend into per-symbol clauses.
  3. Translate every TEXT segment / clause via its OWN focused LLM call.
     EQUATION units pass through verbatim.
  4. Reassemble.

Cost: O(text-segments + clauses) LLM calls per equation-bearing chunk
instead of one. The layout is then guaranteed correct because the markers
were never sent through a single combined translation — they survive
verbatim in the output, and the description clauses can't grow / shrink in
ways that change marker positions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from .glossary import clean_translation_text, extract_json_block, merge_terms
from .validation import translation_problem


_EQUATION_TOKEN_RE = re.compile(r"\[EQUATION(?:_\d+)?\]")
_OPAQUE_MARKER_RE = re.compile(r"(?P<eq>\[EQUATION(?:_\d+)?\])|(?P<br>\n)")

# Korean legend headers: '여기서,', '상기 수학식 N에서,', '다만,', …
_LEGEND_HEADER_RE = re.compile(
    r"(?:여기서|상기\s+[^,，。\n]{1,30}에서|다만)[,，]?\s*",
    re.UNICODE,
)
_LEADING_PARAGRAPH_ID_RE = re.compile(r"^(\s*)(\[\d{1,5}\])\s*")

# Boundary at the start of each '<sym>는' / '<sym>은' clause inside a legend.
# Symbol shape allows any Unicode letter (covers Latin/Greek/math-italic) or
# one of the math-bracket-start chars '〖 ( [' as the first char, then up to
# 40 non-whitespace, non-clause-boundary characters. Boundary characters that
# can precede a new symbol: start-of-string, ASCII/fullwidth comma, ASCII/
# fullwidth semicolon, ASCII/fullwidth period, or one of the Korean
# clause-end copulas '이고' / '이며'.
_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?:^|[,，;；。\.]\s*|(?:이고|이며)[,，]?\s*)"
    r"(?P<sym>(?:[^\W\d_]|[〖\(\[])"
    r"[^\s,;，；。\n]{0,40})"
    r"\s*(?:는|은)\s+",
    re.UNICODE,
)


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------

@dataclass
class Unit:
    kind: str          # "text", "equation", or "linebreak"
    payload: str       # text content, or the marker '[EQUATION_N]'


def split_units(chunk_text: str) -> list[Unit]:
    """Split a chunk's Korean text into TEXT / EQUATION units.

    ``[EQUATION_N]`` markers and real source line breaks become opaque,
    pass-through units. Paragraph ID markers such as ``[0075]`` stay in text;
    their presence alone does not imply a line break or a separate segment.
    """
    units: list[Unit] = []
    last = 0
    for m in _OPAQUE_MARKER_RE.finditer(chunk_text):
        if m.start() > last:
            units.append(Unit("text", chunk_text[last:m.start()]))
        if m.group("eq") is not None:
            units.append(Unit("equation", m.group("eq")))
        else:
            units.append(Unit("linebreak", "\n"))
        last = m.end()
    if last < len(chunk_text):
        units.append(Unit("text", chunk_text[last:]))
    return units


def split_into_clauses(text: str) -> list[tuple[str, str]]:
    """Return list of (symbol, korean_clause) pairs inside a legend body.

    Empty list when no '<sym>는/은 ...' pattern is found.
    """
    matches = list(_CLAUSE_BOUNDARY_RE.finditer(text))
    if not matches:
        return []
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        sym = m.group("sym").strip(" ,；;。.")
        if not sym:
            continue
        clause_start = m.start("sym")
        clause_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        clause = text[clause_start:clause_end].strip(" ,，;；。.\n")
        if clause:
            out.append((sym, clause))
    return out


# ---------------------------------------------------------------------------
# Per-unit translation
# ---------------------------------------------------------------------------

def _llm_text(
    client,
    messages: list[dict],
    *,
    progress=None,
    label: str = "segment",
) -> str:
    """Run one LLM call and return cleaned plain text, or '' on failure."""
    active = list(messages)
    last_problem = ""
    for attempt in range(4):
        try:
            raw = client.complete(active)
        except Exception as exc:
            last_problem = f"The model call failed: {type(exc).__name__}: {exc}"
        else:
            data = extract_json_block(raw) or {}
            text = clean_translation_text(data.get("text")) or clean_translation_text(raw)
            problem = translation_problem(text)
            if not problem:
                return text
            last_problem = problem

        if progress is not None:
            suffix = " Retrying..." if attempt < 3 else " Giving up."
            progress(f"  retry {label}: {last_problem}{suffix}")
        if attempt < 2:
            active = active + [{
                "role": "user",
                "content": (
                    "The previous response was invalid. "
                    f"Problem: {last_problem}\n"
                    "Return only the English translation fragment. "
                    "Do not use JSON, markdown, notes, or Korean text."
                ),
            }]
        elif attempt == 2:
            korean = active[-1].get("content", "")
            active = [
                {
                    "role": "system",
                    "content": "Translate Korean patent text into USPTO-style English. Output plain English only.",
                },
                {
                    "role": "user",
                    "content": (
                        "Translate this Korean patent text fragment. "
                        "Do not output JSON, markdown, notes, or Korean text.\n\n"
                        f"{korean}"
                    ),
                },
            ]
    return ""


def translate_text_segment(
    client,
    korean: str,
    glossary: dict,
    *,
    build_segment_messages: Callable[[str, dict], list[dict]],
    build_clause_messages: Callable[[str, str, dict], list[dict]],
    progress=None,
    label: str = "segment",
) -> str:
    """Translate one inter-equation text segment.

    Outer whitespace is preserved so the assembly step's stitching keeps
    the original layout around equation markers. Inside, if the segment
    is, or contains, a Korean parameter legend ('여기서, …'), the legend
    portion is translated clause-by-clause; any prefix before the legend
    is translated as one piece. Otherwise the whole segment goes through
    a single LLM call.
    """
    if not korean.strip():
        return korean

    id_match = _LEADING_PARAGRAPH_ID_RE.match(korean)
    id_leading = ""
    id_prefix = ""
    if id_match:
        id_leading = id_match.group(1)
        id_prefix = id_match.group(2)
        korean = korean[id_match.end():]
        if not korean.strip():
            return id_leading + id_prefix

    lstripped = korean.lstrip()
    leading_ws = korean[: len(korean) - len(lstripped)]
    rstripped = lstripped.rstrip()
    trailing_ws = lstripped[len(rstripped):]
    body = rstripped

    legend = _LEGEND_HEADER_RE.search(body)
    if legend is None:
        en = _llm_text(
            client,
            build_segment_messages(body, glossary),
            progress=progress,
            label=label,
        )
        translated = leading_ws + en + trailing_ws
        if id_prefix:
            return id_leading + id_prefix + " " + translated.lstrip()
        return translated

    pre = body[: legend.start()].strip(" ,，;；。.\n")
    legend_body = body[legend.end():].strip()

    pre_en = ""
    if pre:
        pre_en = _llm_text(
            client,
            build_segment_messages(pre, glossary),
            progress=progress,
            label=f"{label} preface",
        )

    clauses = split_into_clauses(legend_body)
    if not clauses:
        legend_en = _llm_text(
            client,
            build_segment_messages(body, glossary),
            progress=progress,
            label=f"{label} legend",
        )
    else:
        translated = []
        for sym, ko_clause in clauses:
            en = _llm_text(
                client,
                build_clause_messages(sym, ko_clause, glossary),
                progress=progress,
                label=f"{label} clause {sym}",
            )
            if not en:
                en = f"{sym} is …"
            elif sym not in en:
                en = f"{sym} is {en.lstrip('is ').lstrip('are ').strip()}"
            translated.append(en)
        legend_en = "wherein " + "; ".join(translated)

    if pre_en and legend_en:
        combined = f"{pre_en}, {legend_en}"
    else:
        combined = pre_en or legend_en
    translated = leading_ws + combined + trailing_ws
    if id_prefix:
        return id_leading + id_prefix + " " + translated.lstrip()
    return translated


def translate_chunk_by_sentence(
    chunk_text: str,
    client,
    glossary: dict,
    *,
    build_segment_messages: Callable[[str, dict], list[dict]],
    build_clause_messages: Callable[[str, str, dict], list[dict]],
    progress=None,
    label: str = "chunk",
) -> Optional[str]:
    """Translate a marker-bearing chunk one unit at a time.

    Returns the assembled English (with ``[EQUATION_N]`` markers and source
    line breaks preserved in their original positions) on success, or None
    when no opaque markers are present (so the caller can fall back to the
    chunk-level path).
    """
    units = split_units(chunk_text)
    if not any(u.kind in ("equation", "linebreak") for u in units):
        return None

    out_parts: list[str] = []
    for unit in units:
        if unit.kind == "linebreak":
            out_parts.append("\n")
        elif unit.kind == "equation":
            # Korean doesn't require whitespace before a math element or
            # equation marker; English does. Insert a space when the previous
            # part does not already end with whitespace.
            if out_parts and not out_parts[-1].endswith((" ", "\t", "\n")):
                out_parts.append(" ")
            out_parts.append(unit.payload)
        else:
            translated = translate_text_segment(
                client, unit.payload, glossary,
                build_segment_messages=build_segment_messages,
                build_clause_messages=build_clause_messages,
                progress=progress,
                label=label,
            )
            if (
                out_parts
                and out_parts[-1].endswith("]")
                and translated
                and not translated[0].isspace()
            ):
                out_parts.append(" ")
            out_parts.append(translated)
    return "".join(out_parts)


def is_equation_bearing(chunk_text: str) -> bool:
    return bool(_EQUATION_TOKEN_RE.search(chunk_text))


def needs_per_segment_translation(chunk_text: str) -> bool:
    """Route through the sentence-level path for equations or real line breaks."""
    return is_equation_bearing(chunk_text) or "\n" in chunk_text
