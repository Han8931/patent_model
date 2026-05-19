"""DOCX read/scan and write/apply helpers.

Two halves:
  * input side — section detection, paragraph classification (``scan_paragraphs``).
  * output side — in-place run-rewrite that preserves runs holding images and
    equations, forces Times New Roman on every touched run, and applies
    USPTO-style line breaks + tab indents to claims.
"""

from __future__ import annotations

import re
from typing import Callable

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUTPUT_FONT = "Times New Roman"

#: Korean section header → English label.
SECTION_MAP: dict[str, str] = {
    "[발명의 설명]":                     "DESCRIPTION",
    "[발명의 명칭]":                     "TITLE OF INVENTION",
    "[기술분야]":                        "TECHNICAL FIELD",
    "[배경기술]":                        "BACKGROUND ART",
    "[발명의 내용]":                     "SUMMARY OF INVENTION",
    "[기술적 과제]":                     "TECHNICAL PROBLEM",
    "[과제의 해결 수단]":                "SOLUTION TO PROBLEM",
    "[발명의 효과]":                     "ADVANTAGEOUS EFFECTS OF INVENTION",
    "[도면의 간단한 설명]":              "BRIEF DESCRIPTION OF THE DRAWINGS",
    "[발명의 실시를 위한 구체적인 내용]": "DETAILED DESCRIPTION OF EMBODIMENTS",
    "[대표도]":                          "REPRESENTATIVE FIGURE",
    "[부호의 설명]":                     "EXPLANATION OF REFERENCES",
    "[특허청구범위]":                    "CLAIMS",
    "[청구범위]":                        "CLAIMS",
    "[요약서]":                          "ABSTRACT",
    "[요약]":                            "ABSTRACT",
}

#: Matches a Korean claim anchor like ``【청구항 3】`` or ``[청구항 3]``.
CLAIM_HEADER_RE = re.compile(r"^[【\[]\s*청구항\s*(\d+)\s*[】\]]\s*")

#: Tags that mark a paragraph as media-bearing (image / equation / drawing).
_MEDIA_TAGS = (qn("w:drawing"), qn("w:pict"), qn("m:oMath"), qn("m:oMathPara"))

Progress = Callable[[str], None]


# ---------------------------------------------------------------------------
# Section header detection (with Hangul-signature fallback)
# ---------------------------------------------------------------------------

def _hangul_sig(text: str) -> str:
    """Return only the Hangul syllables in *text* (used for header lookup)."""
    return "".join(c for c in text if "가" <= c <= "힣")


_SECTION_BY_SIG: dict[str, str] = {}
for _phrase, _label in SECTION_MAP.items():
    _sig = _hangul_sig(_phrase)
    if _sig:
        _SECTION_BY_SIG.setdefault(_sig, _label)


def detect_section(text: str) -> str | None:
    """Return the English section label if *text* is a section header.

    Tries an exact-match first, then a Hangul-only signature so variants like
    '[ 청구범위 ]' or '【청구범위】 (Claims)' also resolve correctly.
    """
    stripped = text.strip()
    if not stripped:
        return None
    if stripped in SECTION_MAP:
        return SECTION_MAP[stripped]
    if len(stripped) > 60:
        return None
    return _SECTION_BY_SIG.get(_hangul_sig(stripped))


# ---------------------------------------------------------------------------
# Paragraph scan
# ---------------------------------------------------------------------------

def _has_media(p: Paragraph) -> bool:
    """True if *p* contains an inline image, legacy picture, or OMML equation."""
    el = p._element
    for tag in _MEDIA_TAGS:
        if el.find(f".//{tag}") is not None:
            return True
    return False


def scan_paragraphs(doc) -> list[dict]:
    """Walk the document and tag each paragraph with its section + role.

    role ∈ {"header", "claim", "abstract", "description", "blank", "media"}

    A paragraph that contains an image or an equation is classified as
    ``"media"`` regardless of its Korean text content. Those paragraphs are
    never translated and never rewritten — only the surrounding text-only
    paragraphs flow through the LLM. This protects equations and figures from
    being clobbered by the text-only rewrite path.
    """
    records: list[dict] = []
    current: str | None = None
    for i, p in enumerate(doc.paragraphs):
        text = p.text
        section = detect_section(text)
        if section is not None:
            current = section
            records.append({"index": i, "section": section, "role": "header", "text": text.strip()})
            continue
        if _has_media(p):
            records.append({"index": i, "section": current, "role": "media", "text": text})
            continue
        if not text.strip():
            records.append({"index": i, "section": current, "role": "blank", "text": ""})
            continue
        if current == "CLAIMS":
            role = "claim"
        elif current == "ABSTRACT":
            role = "abstract"
        else:
            role = "description"
        records.append({"index": i, "section": current, "role": role, "text": text})
    return records


def collect_block(records: list[dict], role: str) -> str:
    """Join all paragraphs of a given role into one Korean block."""
    parts = [r["text"] for r in records if r["role"] == role and r["text"]]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Run / paragraph rewrite primitives
# ---------------------------------------------------------------------------

def _set_run_font(run, name: str = OUTPUT_FONT) -> None:
    """Force *run* to use *name* across every Word font slot.

    Word picks a font based on character script: ``ascii`` for Latin, ``hAnsi``
    for high-ANSI, ``eastAsia`` for CJK, ``cs`` for complex scripts. Setting
    only ``run.font.name`` leaves CJK characters rendering in the source's
    Korean font, so any untranslated text would stick out. We set all four.
    """
    run.font.name = name
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    for slot in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rFonts.set(qn(slot), name)


def set_paragraph_text(p: Paragraph, new_text: str) -> None:
    """Replace a paragraph's text in place; embedded "\\n" become soft breaks.

    Every run we write (existing or new) is forced to ``OUTPUT_FONT``. Soft
    breaks are attached to the previous line's run so multi-line claims
    render correctly instead of bunching every break at the top.
    """
    lines = (new_text or "").split("\n")

    if p.runs:
        last = p.runs[0]
        last.text = lines[0]
        _set_run_font(last)
        for r in p.runs[1:]:
            r.text = ""
    else:
        last = p.add_run(lines[0])
        _set_run_font(last)

    for line in lines[1:]:
        last.add_break()
        last = p.add_run(line)
        _set_run_font(last)


def clear_paragraph(p: Paragraph) -> None:
    """Wipe the visible text of *p* without removing runs (so media survive)."""
    for r in p.runs:
        r.text = ""


def apply_output_font(doc) -> None:
    """Sweep every run in body paragraphs and tables, forcing ``OUTPUT_FONT``.

    Catches anything ``set_paragraph_text`` didn't touch: untouched section
    headers, untranslated claim anchors left in Korean, table contents, etc.
    """
    for p in doc.paragraphs:
        for run in p.runs:
            _set_run_font(run)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    for run in p.runs:
                        _set_run_font(run)


# ---------------------------------------------------------------------------
# Claim formatting + apply functions
# ---------------------------------------------------------------------------

def format_claim_uspto(text: str) -> str:
    """Apply USPTO-style line breaks and indentation to a single claim.

    - Break after every ``;`` or ``:`` that's followed by more text.
    - Indent every continuation line with a single tab. The first line
      (claim-number + preamble) stays unindented.

    Whitespace is normalized first so existing line breaks in the LLM output
    don't compound with the ones we add.
    """
    if not text:
        return text
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"([;:])\s+(?=\S)", r"\1\n", text)
    lines = text.split("\n")
    if len(lines) <= 1:
        return text
    return "\n".join([lines[0]] + ["\t" + line.strip() for line in lines[1:]])


def apply_claims(doc, records: list[dict], claims: dict[str, str], *, progress: Progress = print) -> None:
    """Place each English claim onto the paragraph that held its 【청구항 N】 header.

    Each claim is reformatted via :func:`format_claim_uspto` before being
    written. Other paragraphs in the CLAIMS section are cleared so the
    original Korean element-list paragraphs don't bleed through under the
    English claim.
    """
    claim_records = [r for r in records if r["role"] == "claim"]
    if not claim_records:
        return

    anchors: dict[str, int] = {}
    for r in claim_records:
        m = CLAIM_HEADER_RE.match(r["text"])
        if m:
            anchors[m.group(1)] = r["index"]

    # Fallback: no 【청구항 N】 markers found — write everything into the first
    # claim paragraph rather than dropping the translation on the floor.
    if not anchors:
        first = claim_records[0]["index"]
        ordered = sorted(claims, key=lambda s: int(s))
        all_text = "\n\n".join(format_claim_uspto(claims[k]) for k in ordered)
        set_paragraph_text(doc.paragraphs[first], all_text)
        for r in claim_records[1:]:
            clear_paragraph(doc.paragraphs[r["index"]])
        return

    written: set[int] = set()
    untranslated: list[str] = []
    for num, idx in anchors.items():
        text = (claims.get(num) or "").strip()
        if not text:
            untranslated.append(num)
            continue
        set_paragraph_text(doc.paragraphs[idx], format_claim_uspto(text))
        written.add(idx)

    # Continuation paragraphs (non-anchor) are cleared because the English claim
    # text already contains all elements. Anchor paragraphs whose claim never
    # produced an English translation keep their Korean text so the failure is
    # visible to the reviewer instead of silently disappearing.
    for r in claim_records:
        if r["index"] in written:
            continue
        if CLAIM_HEADER_RE.match(r["text"]):
            continue
        clear_paragraph(doc.paragraphs[r["index"]])

    if untranslated:
        progress(f"      WARNING: {len(untranslated)} claim(s) without translation "
                 f"({untranslated}); Korean left in place")


def apply_abstract(doc, records: list[dict], english: str) -> None:
    """Write *english* into the first abstract paragraph; clear the rest."""
    targets = [r for r in records if r["role"] == "abstract"]
    if not targets:
        return
    if not english:
        for r in targets:
            clear_paragraph(doc.paragraphs[r["index"]])
        return
    set_paragraph_text(doc.paragraphs[targets[0]["index"]], english)
    for r in targets[1:]:
        clear_paragraph(doc.paragraphs[r["index"]])


def apply_section_headers(doc, records: list[dict]) -> None:
    """Replace each detected Korean section header with its English label."""
    for r in records:
        if r["role"] == "header" and r.get("section"):
            set_paragraph_text(doc.paragraphs[r["index"]], f"[{r['section']}]")
