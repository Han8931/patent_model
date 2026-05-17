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


_EQUATION_TOKEN_RE = re.compile(r"\[EQUATION(?:_\d+)?\]")
# Paragraph-ID markers like '[0075]'. 1–5 digits; we deliberately match anywhere
# in the text (not just the start) so a line-broken Word paragraph that contains
# multiple IDs has each ID surface as its own opaque unit during reassembly.
_PARAGRAPH_ID_INLINE_RE = re.compile(r"\[\d{1,5}\]")
# Combined matcher used by split_units — recognizes either marker shape so we
# can route over them in a single pass without worrying about overlap.
_OPAQUE_MARKER_RE = re.compile(
    r"(?P<eq>\[EQUATION(?:_\d+)?\])|(?P<pid>\[\d{1,5}\])"
)

# Korean parameter-legend headers. Keep this intentionally narrow: ordinary
# prose often contains phrases like "상기 구조체에서" ("in the structure"),
# which must not be treated as an equation legend.
_LEGEND_HEADER_RE = re.compile(
    r"(?:"
    r"여기서|다만|"
    r"(?:상기\s+)?(?:(?:수학식|관계식|공식|방정식|등식|부등식)"
    r"\s*[^,，。\n]{0,20}|식\s*(?:\d+|[A-Za-z]|\([^)]+\))?)에서"
    r")[,，]?\s*",
    re.UNICODE,
)

# Boundary at the start of each '<sym>는' / '<sym>은' clause inside a legend.
# Symbol shape allows any Unicode letter (covers Latin/Greek/math-italic) or
# one of the math-bracket-start chars '〖 ( [' as the first char, then a short
# inline expression such as 'α² + 1/2'. Boundary characters that can precede a
# new symbol: start-of-string, ASCII/fullwidth comma, ASCII/fullwidth semicolon,
# ASCII/fullwidth period, or one of the Korean clause-end copulas '이고' / '이며'.
_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?:^|[,，;；。\.]\s*|(?:이고|이며)[,，]?\s*)"
    r"(?P<sym>(?:[^\W\d_]|[〖\(\[])"
    r"(?:(?!\s*(?:는|은)\s+)[^,;，；。\n]){0,80}?)"
    r"\s*(?:는|은)\s+",
    re.UNICODE,
)


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------

@dataclass
class Unit:
    kind: str          # "text" or "equation"
    payload: str       # text content, or the marker '[EQUATION_N]'


def split_units(chunk_text: str) -> list[Unit]:
    """Split a chunk's Korean text into TEXT / EQUATION / PARA_ID units.

    Both ``[EQUATION_N]`` (Word-equation placeholder) and ``[NNN]`` (Korean
    patent paragraph ID — 1–5 digits) become opaque, pass-through units.
    Everything else (including newlines) becomes a TEXT unit that we translate.
    """
    units: list[Unit] = []
    last = 0
    for m in _OPAQUE_MARKER_RE.finditer(chunk_text):
        if m.start() > last:
            units.append(Unit("text", chunk_text[last:m.start()]))
        if m.group("eq") is not None:
            units.append(Unit("equation", m.group("eq")))
        else:
            units.append(Unit("para_id", m.group("pid")))
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

_HANGUL_RE = re.compile(r"[가-힯]")


def _has_hangul(text: str) -> bool:
    return bool(text and _HANGUL_RE.search(text))


def _llm_text(client, messages: list[dict]) -> str:
    """Run one LLM call → cleaned English text. Retries once on empty / Korean.

    Tries three response shapes:
      1. JSON ``{"text": ...}`` envelope.
      2. Plain text with a trailing ``===== GLOSSARY =====`` banner.
      3. Plain text only.

    If the cleaned result is empty OR still contains Hangul, send ONE
    corrective follow-up message ("English only — no Korean characters")
    and re-parse. If that retry also still has Hangul, return "" so the
    caller treats this segment as a translation failure rather than
    propagating Korean into the output.
    """
    def _attempt(msgs: list[dict]) -> str:
        try:
            raw = client.complete(msgs)
        except Exception:
            return ""
        # Shape 1: JSON envelope.
        data = extract_json_block(raw) or {}
        text = clean_translation_text(data.get("text"))
        if text:
            return text
        # Shapes 2 + 3: plain text, possibly with a GLOSSARY trailer.
        from .prompts import parse_translation_with_glossary
        translation, _new_terms = parse_translation_with_glossary(raw)
        return clean_translation_text(translation)

    text = _attempt(messages)
    if text and not _has_hangul(text):
        return text

    # Retry once with a corrective follow-up. The pipeline is impeccable-
    # quality: any Korean leftover triggers a retry, and if the retry still
    # has Korean we return "" so upstream code can mark the chunk failed.
    problem = "empty/placeholder response" if not text else "response still contained Korean characters"
    retry_msgs = messages + [{
        "role": "user",
        "content": (
            "Your previous response was unusable. "
            f"Problem: {problem}. "
            "Translate the Korean above to English in USPTO style. "
            "Output English only — NO Korean characters anywhere, no markdown, "
            "no commentary. Keep any [EQUATION_N] marker verbatim."
        ),
    }]
    text = _attempt(retry_msgs)
    if text and _has_hangul(text):
        return ""
    return text


def translate_text_segment(
    client,
    korean: str,
    glossary: dict,
    *,
    build_segment_messages: Callable[[str, dict], list[dict]],
    build_clause_messages: Callable[[str, str, dict], list[dict]],
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

    lstripped = korean.lstrip()
    leading_ws = korean[: len(korean) - len(lstripped)]
    rstripped = lstripped.rstrip()
    trailing_ws = lstripped[len(rstripped):]
    body = rstripped

    legend = _LEGEND_HEADER_RE.search(body)
    if legend is None:
        en = _llm_text(client, build_segment_messages(body, glossary))
        return leading_ws + en + trailing_ws

    pre = body[: legend.start()].strip(" ,，;；。.\n")
    legend_body = body[legend.end():].strip()

    pre_en = ""
    if pre:
        pre_en = _llm_text(client, build_segment_messages(pre, glossary))

    clauses = split_into_clauses(legend_body)
    if not clauses:
        legend_en = _llm_text(client, build_segment_messages(body, glossary))
    else:
        translated = []
        for sym, ko_clause in clauses:
            en = _llm_text(
                client,
                build_clause_messages(sym, ko_clause, glossary),
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
    return leading_ws + combined + trailing_ws


def translate_chunk_by_sentence(
    chunk_text: str,
    client,
    glossary: dict,
    *,
    build_segment_messages: Callable[[str, dict], list[dict]],
    build_clause_messages: Callable[[str, str, dict], list[dict]],
) -> Optional[str]:
    """Translate a marker-bearing chunk one unit at a time.

    Returns the assembled English (with ``[EQUATION_N]`` and ``[NNN]`` markers
    preserved in their original positions) on success, or None when no opaque
    markers are present (so the caller can fall back to the chunk-level path).
    """
    units = split_units(chunk_text)
    if not any(u.kind in ("equation", "para_id") for u in units):
        return None

    out_parts: list[str] = []
    for unit in units:
        if unit.kind in ("equation", "para_id"):
            # Korean doesn't require whitespace before a math element or
            # paragraph ID; English does. Insert a space when the previous
            # part doesn't already end with whitespace.
            if out_parts and not out_parts[-1].endswith((" ", "\t", "\n")):
                out_parts.append(" ")
            out_parts.append(unit.payload)
        else:
            translated = translate_text_segment(
                client, unit.payload, glossary,
                build_segment_messages=build_segment_messages,
                build_clause_messages=build_clause_messages,
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


def has_inline_paragraph_id(chunk_text: str) -> bool:
    """True when the chunk text has a '[NNN]' paragraph ID *inside* it.

    Used to detect line-break-induced multi-paragraph chunks: chunk_body
    already strips a single HEAD '[NNN]' into ``chunk.paragraph_id_prefix``,
    so any '[NNN]' that survives into the chunk text came from a `<w:br>`
    in the source — that's the line-breaking case that needs sentence-level
    splitting so each '[NNN]' stays adjacent to its own body content.
    """
    return bool(_PARAGRAPH_ID_INLINE_RE.search(chunk_text))


def needs_per_segment_translation(chunk_text: str) -> bool:
    """Combined router: send through the sentence-level path when the chunk
    has either inline equations or extra paragraph IDs surviving the head
    strip."""
    return is_equation_bearing(chunk_text) or has_inline_paragraph_id(chunk_text)
