"""docx manipulation helpers — pure functions, no state."""

from __future__ import annotations

import re
from copy import deepcopy

from docx.oxml import OxmlElement
from docx.oxml.ns import qn


_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
_XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'
_HANGUL_RE = re.compile(r'[가-힯]')
_EQUATION_PLACEHOLDER = '[EQUATION]'
_EQUATION_TOKEN_RE = re.compile(r'\[EQUATION(?:_\d+)?\]')
_EQUATION_PLACEHOLDER_RE = re.compile(r'\s*\[EQUATION(?:_\d+)?\]\s*')


def has_drawing(para) -> bool:
    """True iff the paragraph contains a real <w:drawing> (image/figure)."""
    return para._p.find('.//{%s}drawing' % _W) is not None


def has_math(para) -> bool:
    """True iff the paragraph contains an OMML equation."""
    return (
        para._p.find('.//{%s}oMath' % _M) is not None
        or para._p.find('.//{%s}oMathPara' % _M) is not None
    )


def has_non_text_content(para) -> bool:
    """Backwards-compat alias — true if the paragraph has math OR drawing."""
    return has_drawing(para) or has_math(para)


def text_runs(para) -> list:
    return [r for r in para.runs if r.text]


def extract_all_text(para) -> str:
    """Concatenate text from <w:t> and translatable <m:t> in document order.

    Korean equation paragraphs often embed Korean labels or 'where ...' clauses
    inside <m:t> elements; para.text only returns <w:t> content and misses them.
    Formula-only Word equations are exposed as [EQUATION] placeholders instead
    of raw math symbols so the LLM does not translate or duplicate the formula.
    Use a w:br as a soft separator to mirror Word's visual line breaks.
    """
    parts: list[str] = []
    _append_translatable_text(para._p, parts)
    return ''.join(parts)


def _local_name(el) -> str:
    tag = el.tag
    return tag.split('}')[-1] if '}' in tag else tag


def _is_math_element(el) -> bool:
    return el.tag in (f'{{{_M}}}oMath', f'{{{_M}}}oMathPara')


def _element_text(el) -> str:
    return ''.join(
        child.text or ''
        for child in el.iter()
        if _local_name(child) == 't'
    )


def _append_translatable_text(el, parts: list[str]) -> None:
    """Append paragraph text while treating each top-level equation as atomic."""
    if _is_math_element(el):
        math_text = _element_text(el)
        if not math_text:
            return
        if _HANGUL_RE.search(math_text):
            parts.append(math_text)
        else:
            parts.append(_EQUATION_PLACEHOLDER)
        return

    local = _local_name(el)
    if local == 't':
        if el.text:
            parts.append(el.text)
        return
    if local == 'br':
        parts.append('\n')
        return

    for child in el:
        _append_translatable_text(child, parts)


def _top_level_math_elements(para) -> list:
    elements: list = []

    def visit(el, inside_math: bool = False) -> None:
        is_math = _is_math_element(el)
        if is_math and not inside_math:
            elements.append(el)
            return
        for child in el:
            visit(child, inside_math or is_math)

    visit(para._p)
    return elements


def _remove_korean_math(para) -> None:
    """Remove equation XML whose own text contains Korean.

    Formula-only equations are preserved. Equations containing Korean are removed
    after the English translation is written; otherwise Word-equation text stored
    in <m:t> remains visible because python-docx text runs do not own it.
    """
    for el in list(_top_level_math_elements(para)):
        if not _HANGUL_RE.search(_element_text(el)):
            continue
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _strip_equation_placeholders(text: str) -> str:
    return _EQUATION_PLACEHOLDER_RE.sub(' ', text).strip()


def consolidate_formula_math_into(target_para, source_paras) -> int:
    """Detach formula <m:oMath> from each source paragraph and append to target.

    Used when a translation chunk spans multiple paragraphs but the equations
    live in trailing paragraphs. Moving them into the head paragraph lets
    _replace_text_with_math_placeholders interleave the translation around
    them inside a single paragraph, keeping equations visually aligned with
    the English text that references them.

    Equations whose text contains Hangul are NOT moved — they will be removed
    by _remove_korean_math during the head's replace_text call (their Korean
    content is replaced by the translation in the head).

    Returns the number of equations moved.
    """
    moved = 0
    target_p = target_para._p
    for src_para in source_paras:
        for math_el in list(_top_level_math_elements(src_para)):
            if _HANGUL_RE.search(_element_text(math_el)):
                continue  # Korean math will be removed elsewhere
            parent = math_el.getparent()
            if parent is not None:
                parent.remove(math_el)
            target_p.append(math_el)
            moved += 1
    return moved


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


def _build_text_run(text: str, font_name: str):
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
    return new_r


def _replace_text_with_math_placeholders(para, new_text: str, font_name: str) -> bool:
    formula_math = [
        el for el in _top_level_math_elements(para)
        if not _HANGUL_RE.search(_element_text(el))
    ]
    if not formula_math or not _EQUATION_TOKEN_RE.search(new_text):
        return False

    parts = _EQUATION_TOKEN_RE.split(new_text, maxsplit=len(formula_math))
    if len(parts) < 2:
        return False
    while len(parts) < len(formula_math) + 1:
        parts.append('')
    parts = [_EQUATION_TOKEN_RE.sub('', part) for part in parts]

    # Clear existing normal text. The equation XML remains in its original spot.
    for run in para.runs:
        write_run_with_breaks(run, '', font_name)

    for idx, math_el in enumerate(formula_math):
        before = parts[idx]
        if before:
            math_el.addprevious(_build_text_run(before, font_name))

    after = parts[len(formula_math)]
    if after:
        formula_math[-1].addnext(_build_text_run(after, font_name))
    return True


def replace_text(para, new_text: str, font_name: str) -> None:
    """Write new_text into the paragraph's text runs.

    Formula-only equations are preserved. Equations containing Korean text are
    removed after their English translation is written; otherwise the original
    Korean <m:t> text remains visible because python-docx text runs do not own it.
    """
    _remove_korean_math(para)
    if _replace_text_with_math_placeholders(para, new_text, font_name):
        return

    new_text = _strip_equation_placeholders(new_text)
    runs = text_runs(para)
    if not runs:
        if not new_text:
            return
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
    new_r = _build_text_run(text, font_name)

    # Insert after <w:pPr> if present, otherwise at the start
    p = para._p
    pPr = p.find(qn('w:pPr'))
    if pPr is not None:
        pPr.addnext(new_r)
    else:
        p.insert(0, new_r)


def insert_para_after(
    ref_para,
    text: str,
    font_name: str,
    *,
    format_ref_para=None,
) -> None:
    """Insert a new paragraph immediately after ``ref_para``.

    If ``format_ref_para`` is given, clone its <w:pPr> onto the new paragraph
    so the inserted paragraph inherits alignment, indentation, spacing, and
    style. This matters when the new paragraph sits between centered equation
    paragraphs and a left-aligned legend — without a format reference, the
    new paragraph uses Word defaults and visually clashes.
    """
    new_p = OxmlElement('w:p')

    if format_ref_para is not None:
        ref_pPr = format_ref_para._p.find(qn('w:pPr'))
        if ref_pPr is not None:
            new_p.append(deepcopy(ref_pPr))

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


# ---------------------------------------------------------------------------
# 'respectively' expansion
# ---------------------------------------------------------------------------
# Defense-in-depth for the parameter-legend layout. The prompt already tells
# the LLM not to collapse parameters into 'A, B, C are X, Y, Z, respectively',
# but if it slips through we rewrite into per-parameter clauses joined by ';'.
#
# Conservative on purpose: we only fire when
#   - the symbol list is clearly math-like (short identifiers / Greek letters),
#   - each value is a simple noun phrase with no internal commas or semicolons,
#   - the symbol count and value count match,
#   - the trailing word is the literal 'respectively' (with optional comma).
# Anything fancier is left alone — better to skip a repair than to mangle text.

# Identifier shape: 1–10 chars, starts with a letter (ASCII or Greek), allows
# digits, subscripts, primes, underscores. Subscript characters U+2080..U+2089.
_SYM = r'[A-Za-zΑ-Ωα-ω][A-Za-z0-9Α-Ωα-ω₀-₉_\']{0,9}'
_VAL = r"[^,;.]+"

_RESPECTIVELY_RE = re.compile(
    rf'(?P<symbols>{_SYM}'
    rf'(?:\s*,\s*{_SYM}){{1,8}}'
    rf'(?:\s*,?\s*and\s+{_SYM})?)'
    r'\s+(?P<verb>are|denote|represent|stand\s+for|indicate)\s+'
    rf'(?P<values>{_VAL}'
    rf'(?:\s*,\s*{_VAL}){{1,8}}'
    rf'(?:\s*,?\s*and\s+{_VAL})?)'
    r'\s*,?\s*respectively',
)

_VERB_SINGULAR = {
    "are": "is",
    "denote": "denotes",
    "represent": "represents",
    "stand for": "stands for",
    "indicate": "indicates",
}


def _split_list(text: str) -> list[str]:
    parts = re.split(r'\s*,\s*and\s+|\s+and\s+|\s*,\s*', text.strip())
    return [p.strip() for p in parts if p.strip()]


def _expand_respectively(text: str) -> str:
    def repl(m: re.Match) -> str:
        symbols = _split_list(m.group("symbols"))
        values = _split_list(m.group("values"))
        if len(symbols) != len(values) or len(symbols) < 2:
            return m.group(0)
        verb = re.sub(r'\s+', ' ', m.group("verb").lower())
        singular = _VERB_SINGULAR.get(verb, verb)
        clauses = [f"{s} {singular} {v}" for s, v in zip(symbols, values)]
        return "; ".join(clauses)

    return _RESPECTIVELY_RE.sub(repl, text)


def postprocess(text: str) -> str:
    text = _normalize_unicode(text)
    text = _expand_respectively(text)
    text = _break_sentences(text)
    text = _break_after_semicolons(text)
    return text
