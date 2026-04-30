"""Classify node — assign kind + section to every paragraph."""

from __future__ import annotations

from ..docx_utils import has_non_text_content, text_runs
from ..sections import (
    BLANK_RE,
    CLAIM_HEADER_RE,
    SECTION_HEADER_MAP,
)
from ..state import ParagraphRecord, TranslationState


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
