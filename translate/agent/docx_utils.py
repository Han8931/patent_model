"""docx manipulation helpers — pure functions, no state."""

from __future__ import annotations

import re

from docx.oxml import OxmlElement
from docx.oxml.ns import qn


_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
_XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'


def has_non_text_content(para) -> bool:
    p = para._p
    return (
        p.find('.//{%s}drawing' % _W) is not None or
        p.find('.//{%s}oMath' % _M) is not None
    )


def text_runs(para) -> list:
    return [r for r in para.runs if r.text]


def write_run_with_breaks(run, text: str, font_name: str) -> None:
    """Replace run content; convert \\n into <w:br/> elements."""
    r = run._r
    for child in list(r):
        local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if local in ('t', 'br'):
            r.remove(child)

    parts = text.split('\n')
    for idx, part in enumerate(parts):
        t = OxmlElement('w:t')
        if part.startswith(' ') or part.endswith(' '):
            t.set(_XML_SPACE, 'preserve')
        t.text = part
        r.append(t)
        if idx < len(parts) - 1:
            r.append(OxmlElement('w:br'))

    run.font.name = font_name


def replace_text(para, new_text: str, font_name: str) -> None:
    """Write new_text into the paragraph's text runs, leaving non-text XML untouched.

    If the paragraph has no text runs but DOES have non-text content (e.g. an
    equation), a new <w:r><w:t>...</w:t></w:r> is inserted at the start so the
    translation is rendered alongside the equation.
    """
    runs = text_runs(para)
    if not runs:
        if not new_text:
            return
        if has_non_text_content(para):
            _prepend_text_run(para, new_text, font_name)
        return
    write_run_with_breaks(runs[0], new_text, font_name)
    for run in runs[1:]:
        run.text = ''
        run.font.name = font_name
    for run in para.runs:
        run.font.name = font_name


def _prepend_text_run(para, text: str, font_name: str) -> None:
    """Insert a new <w:r><w:t>...</w:t></w:r> as the first child of <w:p>."""
    new_r = OxmlElement('w:r')
    new_rpr = OxmlElement('w:rPr')
    new_rFonts = OxmlElement('w:rFonts')
    new_rFonts.set(qn('w:ascii'), font_name)
    new_rFonts.set(qn('w:hAnsi'), font_name)
    new_rpr.append(new_rFonts)
    new_r.append(new_rpr)

    parts = text.split('\n')
    for idx, part in enumerate(parts):
        t = OxmlElement('w:t')
        if part.startswith(' ') or part.endswith(' '):
            t.set(_XML_SPACE, 'preserve')
        t.text = part
        new_r.append(t)
        if idx < len(parts) - 1:
            new_r.append(OxmlElement('w:br'))

    # Insert after <w:pPr> if present, otherwise at the start
    p = para._p
    pPr = p.find(qn('w:pPr'))
    if pPr is not None:
        pPr.addnext(new_r)
    else:
        p.insert(0, new_r)


def insert_para_after(ref_para, text: str, font_name: str) -> None:
    new_p = OxmlElement('w:p')
    new_r = OxmlElement('w:r')
    new_rpr = OxmlElement('w:rPr')
    new_rFonts = OxmlElement('w:rFonts')
    new_rFonts.set(qn('w:ascii'), font_name)
    new_rFonts.set(qn('w:hAnsi'), font_name)
    new_rpr.append(new_rFonts)
    new_r.append(new_rpr)
    new_t = OxmlElement('w:t')
    new_t.text = text
    new_r.append(new_t)
    new_p.append(new_r)
    ref_para._p.addnext(new_p)


def word_count(text: str) -> int:
    return len(text.split())


# ---------------------------------------------------------------------------
# Text post-processing
# ---------------------------------------------------------------------------

_UNICODE_NORMALIZE_MAP = str.maketrans({
    '‑': '-', '‐': '-', '‒': '-', '–': '-', '—': '-',
    ' ': ' ', ' ': ' ', ' ': ' ',
    '‘': "'", '’': "'", '“': '"', '”': '"',
    '…': '...',
})

_ABBREVS = {
    "fig", "figs", "e.g", "i.e", "no", "nos", "u.s", "vol", "approx",
    "cf", "vs", "et al", "sec", "art", "para", "ref", "dept",
}


def _normalize_unicode(text: str) -> str:
    return text.translate(_UNICODE_NORMALIZE_MAP)


def _break_sentences(text: str) -> str:
    protected = re.sub(
        r'\b(' + '|'.join(re.escape(a) for a in _ABBREVS) + r')\.',
        lambda m: m.group(1) + '\x00',
        text,
        flags=re.IGNORECASE,
    )
    broken = re.sub(r'\.\s+(?=[A-Z])', '.\n', protected)
    return broken.replace('\x00', '.')


def _break_after_semicolons(text: str) -> str:
    return re.sub(r';\s+', ';\n', text)


def postprocess(text: str) -> str:
    text = _normalize_unicode(text)
    text = _break_sentences(text)
    text = _break_after_semicolons(text)
    return text
