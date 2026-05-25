"""Classify node — assign kind + section to every paragraph."""

from __future__ import annotations

import json
import re

from ..docx_utils import (
    extract_all_text,
    has_drawing,
    has_math,
    has_non_text_content,
    iter_all_paragraphs,
)
from ..glossary import extract_json_block
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

_SECTION_ALIASES = {
    "TITLE": "TITLE OF INVENTION",
    "TITLE OF INVENTION": "TITLE OF INVENTION",
    "DESCRIPTION": "DESCRIPTION",
    "BODY": "DETAILED DESCRIPTION OF EMBODIMENTS",
    "DETAILED DESCRIPTION": "DETAILED DESCRIPTION OF EMBODIMENTS",
    "DETAILED DESCRIPTION OF EMBODIMENTS": "DETAILED DESCRIPTION OF EMBODIMENTS",
    "TECHNICAL FIELD": "TECHNICAL FIELD",
    "BACKGROUND": "BACKGROUND ART",
    "BACKGROUND ART": "BACKGROUND ART",
    "BRIEF DESCRIPTION": "BRIEF DESCRIPTION OF THE DRAWINGS",
    "BRIEF DESCRIPTION OF THE DRAWINGS": "BRIEF DESCRIPTION OF THE DRAWINGS",
    "TECHNICAL PROBLEM": "TECHNICAL PROBLEM",
    "SOLUTION TO PROBLEM": "SOLUTION TO PROBLEM",
    "ADVANTAGEOUS EFFECTS": "ADVANTAGEOUS EFFECTS OF INVENTION",
    "ADVANTAGEOUS EFFECTS OF INVENTION": "ADVANTAGEOUS EFFECTS OF INVENTION",
    "CLAIM": "CLAIMS",
    "CLAIMS": "CLAIMS",
    "ABSTRACT": "ABSTRACT",
}

_KIND_ALIASES = {
    "section": "section_header",
    "section_header": "section_header",
    "heading": "section_header",
    "claim_header": "claim_header",
    "claim": "text",
    "claim_body": "text",
    "body": "text",
    "text": "text",
    "abstract": "text",
    "title": "text",
    "blank": "blank",
    "image": "image",
}


def _normalize_section(value: str | None) -> str | None:
    if not value:
        return None
    key = re.sub(r"[^A-Z ]+", "", str(value).upper()).strip()
    return _SECTION_ALIASES.get(key)


def _normalize_kind(value: str | None) -> str | None:
    if not value:
        return None
    key = re.sub(r"[^a-z_]+", "_", str(value).lower()).strip("_")
    return _KIND_ALIASES.get(key)


def _llm_classify_records(state: TranslationState, records: list[ParagraphRecord]) -> None:
    """LLM fallback classifier for section/claim detection.

    Rules handle obvious templates, but Korean patent DOCX files vary a lot.
    Since quality is more important than LLM cost here, ask the model to
    classify every Hangul-bearing text/header paragraph and use the result as a
    correction layer. Non-text content is never changed.
    """
    client = state.get("client")
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)
    if client is None:
        return

    candidates = [
        r for r in records
        if r.kind in {"text", "section_header", "claim_header"}
        and _HANGUL_RE.search(r.raw or extract_all_text(r.para) or "")
    ]
    if not candidates:
        return

    progress(f"LLM classifying {len(candidates)} Korean paragraph(s) for section coverage…")
    batch_size = 80
    updates = 0
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start:start + batch_size]
        payload = [
            {
                "id": r.index,
                "current_kind": r.kind,
                "current_section": r.section,
                "text": (r.raw or extract_all_text(r.para) or "")[:1200],
            }
            for r in batch
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "You classify paragraphs from a Korean patent application DOCX. "
                    "Return ONLY valid JSON array. Do not translate. Use context and order. "
                    "Allowed section values: TITLE OF INVENTION, DESCRIPTION, TECHNICAL FIELD, "
                    "BACKGROUND ART, BRIEF DESCRIPTION OF THE DRAWINGS, DETAILED DESCRIPTION OF EMBODIMENTS, "
                    "TECHNICAL PROBLEM, SOLUTION TO PROBLEM, ADVANTAGEOUS EFFECTS OF INVENTION, "
                    "CLAIMS, ABSTRACT. Allowed kind values: section_header, claim_header, text. "
                    "For claim paragraphs, set section to CLAIMS and claim_num when known. "
                    "For section headings, set kind section_header and mapped to the English heading."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Classify these paragraphs. Return JSON array with objects: "
                    "{id, kind, section, mapped, claim_num}.\n\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
            },
        ]
        try:
            raw = client.complete(messages)
            parsed = extract_json_block(raw)
        except Exception as exc:
            if verbose:
                print(f"  classify LLM fallback skipped batch {start}: {type(exc).__name__}: {exc}")
            continue
        if not isinstance(parsed, list):
            if verbose:
                print(f"  classify LLM fallback returned non-list for batch {start}")
            continue

        by_id = {r.index: r for r in batch}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            r = by_id.get(idx)
            if r is None:
                continue
            section = _normalize_section(item.get("section") or item.get("mapped"))
            kind = _normalize_kind(item.get("kind"))
            if section:
                r.section = section
            if kind in {"section_header", "claim_header", "text"}:
                r.kind = kind
            if r.kind == "section_header":
                r.mapped = _normalize_section(item.get("mapped") or item.get("section")) or section
            if r.kind == "claim_header" or section == "CLAIMS":
                r.section = "CLAIMS"
                r.mapped = None if r.kind != "section_header" else r.mapped
                try:
                    claim_num = item.get("claim_num")
                    if claim_num is not None and str(claim_num).strip():
                        r.claim_num = int(claim_num)
                except (TypeError, ValueError):
                    pass
                # If the whole claim is in this paragraph, keep it translatable.
                if r.kind == "claim_header" and (r.raw or "").strip() and not CLAIM_HEADER_RE.match((r.raw or "").strip()):
                    r.kind = "text"
            updates += 1

    if updates:
        progress(f"LLM classification applied to {updates} paragraph record(s)")


def _fail_unclassified_korean(records: list[ParagraphRecord]) -> None:
    missed = [
        r for r in records
        if r.kind == "text"
        and _HANGUL_RE.search(r.raw or extract_all_text(r.para) or "")
        and not r.section
    ]
    if not missed:
        return
    preview = "; ".join(f"{r.index}: {(r.raw or '')[:80]!r}" for r in missed[:8])
    raise RuntimeError(
        f"{len(missed)} Korean paragraph(s) remain unclassified after rule + LLM classification. "
        "Refusing to continue because they could be skipped. "
        f"First occurrence(s): {preview}"
    )


def classify(state: TranslationState) -> dict:
    doc = state["doc"]
    progress = state.get("progress") or (lambda _: None)

    records: list[ParagraphRecord] = []
    current_section: str | None = None

    for idx, para in enumerate(iter_all_paragraphs(doc)):
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
            claim_num = int(next(g for g in m.groups() if g))
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

    _llm_classify_records(state, records)
    _fail_unclassified_korean(records)

    # Per-kind breakdown — surfaces 'classifier saw nothing' or
    # 'no claims_header detected' situations at a glance, so a doc that
    # finishes translation in milliseconds is immediately diagnosable.
    counts: dict[str, int] = {}
    for r in records:
        counts[r.kind] = counts.get(r.kind, 0) + 1
    sections_seen = sorted({r.section for r in records if r.section})
    breakdown = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    progress(
        f"Classified {len(records)} paragraphs "
        f"({breakdown}; sections={sections_seen or 'none-detected'})"
    )
    return {"records": records}
