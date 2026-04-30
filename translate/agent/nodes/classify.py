"""Classify node — assign kind + section to every paragraph."""

from __future__ import annotations

from ..docx_utils import has_non_text_content, text_runs
from ..sections import (
    BLANK_RE,
    CLAIM_HEADER_RE,
    SECTION_HEADER_MAP,
)
from ..state import ParagraphRecord, TranslationState


# Fallback abstract markers — used only when ABSTRACT hasn't been detected yet.
# Korean docs sometimes use just '[요약]' or '【요약】' instead of '[요약서]'.
_ABSTRACT_FALLBACK_MARKERS = {"[요약]", "【요약】"}


def classify(state: TranslationState) -> dict:
    doc = state["doc"]
    progress = state.get("progress") or (lambda _: None)

    records: list[ParagraphRecord] = []
    current_section: str | None = None

    for idx, para in enumerate(doc.paragraphs):
        raw = para.text
        stripped = raw.strip()

        if BLANK_RE.match(raw):
            records.append(ParagraphRecord(
                index=idx, kind="blank", para=para,
                section=current_section,
            ))
            continue

        if has_non_text_content(para) and not text_runs(para):
            records.append(ParagraphRecord(
                index=idx, kind="image", para=para,
                section=current_section,
            ))
            continue

        mapped = SECTION_HEADER_MAP.get(stripped)
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
