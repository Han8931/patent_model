"""Classify node — assign kind + section to every paragraph."""

from __future__ import annotations

import re

from ..docx_utils import (
    extract_all_text,
    has_drawing,
    has_math,
    has_non_text_content,
)
from ..sections import (
    BLANK_RE,
    CLAIM_HEADER_RE,
    SECTION_HEADER_MAP,
    detect_section_header,
)
from ..state import ParagraphRecord, TranslationState

# Hangul block — used to decide whether a math-only paragraph's <m:t> content
# is real Korean text (translate it) or just math notation (preserve as image).
_HANGUL_RE = re.compile(r'[가-힯]')


# Fallback abstract markers — used only when ABSTRACT hasn't been detected yet.
# Korean docs sometimes use just '[요약]' or '【요약】' instead of '[요약서]'.
_ABSTRACT_FALLBACK_MARKERS = {"[요약]", "【요약】"}


def classify(state: TranslationState) -> dict:
    doc = state["doc"]
    progress = state.get("progress") or (lambda _: None)

    records: list[ParagraphRecord] = []
    current_section: str | None = None

    for idx, para in enumerate(doc.paragraphs):
        # Pull text from <w:t> AND <m:t> so equation-embedded Korean is visible.
        raw = extract_all_text(para)
        stripped = raw.strip()

        # Truly blank paragraph (no text anywhere, no drawing, no math).
        if not stripped and not has_non_text_content(para):
            records.append(ParagraphRecord(
                index=idx, kind="blank", para=para,
                section=current_section,
            ))
            continue

        # Real images/figures: <w:drawing>. These have nothing to translate.
        if has_drawing(para) and not stripped:
            records.append(ParagraphRecord(
                index=idx, kind="image", para=para,
                section=current_section,
            ))
            continue

        # Math-only paragraphs: preserve as image when there's no extractable
        # text, OR when the only extractable text is math notation (no Hangul).
        # Math paragraphs with embedded Korean labels fall through to translation.
        if has_math(para) and not has_drawing(para) and not _HANGUL_RE.search(stripped):
            records.append(ParagraphRecord(
                index=idx, kind="image", para=para,
                section=current_section,
            ))
            continue

        mapped = detect_section_header(stripped)
        if mapped:
            if mapped != current_section:
                current_section = mapped
            records.append(ParagraphRecord(
                index=idx, kind="section_header", para=para,
                raw=raw, section=current_section, mapped=mapped,
            ))
            continue

        # Fallback: '[요약]' or '【요약】' starts ABSTRACT when no proper header was seen.
        # Once we're already in ABSTRACT, leave as a text record (chunk_abstract drops it).
        if stripped in _ABSTRACT_FALLBACK_MARKERS and current_section != "ABSTRACT":
            current_section = "ABSTRACT"
            records.append(ParagraphRecord(
                index=idx, kind="section_header", para=para,
                raw=raw, section=current_section, mapped="ABSTRACT",
            ))
            continue

        # Content-based abstract auto-detect: when in CLAIMS but encounter a paragraph
        # that clearly starts an abstract ("본 발명은", "본 발명의"), switch to ABSTRACT.
        if current_section == "CLAIMS" and (
            stripped.startswith("본 발명은") or stripped.startswith("본 발명의")
        ):
            current_section = "ABSTRACT"
            records.append(ParagraphRecord(
                index=idx, kind="text", para=para,
                raw=raw, section=current_section,
                mixed=has_non_text_content(para),
            ))
            continue

        m = CLAIM_HEADER_RE.match(stripped)
        if m:
            if current_section != "CLAIMS":
                current_section = "CLAIMS"
            claim_num = int(m.group(1))
            body = stripped[m.end():]
            if not body:
                records.append(ParagraphRecord(
                    index=idx, kind="claim_header", para=para,
                    raw=raw, section=current_section, claim_num=claim_num,
                ))
            else:
                records.append(ParagraphRecord(
                    index=idx, kind="text", para=para,
                    raw=body, section=current_section,
                    mixed=has_non_text_content(para), claim_num=claim_num,
                ))
            continue

        records.append(ParagraphRecord(
            index=idx, kind="text", para=para,
            raw=raw, section=current_section,
            mixed=has_non_text_content(para),
        ))

    progress(f"Classified {len(records)} paragraphs")
    return {"records": records}
