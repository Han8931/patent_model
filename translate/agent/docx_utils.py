"""docx manipulation helpers — pure functions, no state."""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph


_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
_XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'
_HANGUL_RE = re.compile(r'[가-힯]')
_EQUATION_PLACEHOLDER = '[EQUATION]'
_EQUATION_TOKEN_RE = re.compile(r'\[EQUATION(?:_\d+)?\]')
_EQUATION_PLACEHOLDER_RE = re.compile(r'\s*\[EQUATION(?:_\d+)?\]\s*')


def iter_all_paragraphs(doc):
    """Yield every <w:p> in document body in source order, INCLUDING those
    nested inside tables (and tables-in-tables).

    python-docx's ``doc.paragraphs`` only returns paragraphs that are direct
    children of <w:body>; content placed inside a <w:tbl> is invisible to it.
    Many patent templates wrap the entire description / claims / abstract in
    a single root-level table — for those documents ``doc.paragraphs`` returns
    almost nothing and translation finishes in milliseconds with the source
    text untouched. This iterator walks the body subtree so the classifier
    sees every paragraph regardless of container.

    The yielded objects are real ``docx.text.paragraph.Paragraph`` instances,
    so existing helpers (``replace_text``, ``insert_para_after``, run access)
    work on them unchanged.
    """
    body = doc.element.body
    P_TAG = f'{{{_W}}}p'
    for p_el in body.iter(P_TAG):
        yield Paragraph(p_el, body)


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


def remove_paragraph(para) -> None:
    """Remove a paragraph from the document XML."""
    p = para._p
    parent = p.getparent()
    if parent is not None:
        parent.remove(p)


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


def _append_translatable_text(el, parts: list[str], state: dict | None = None) -> None:
    """Append paragraph text while treating each top-level equation as atomic.

    ``state`` carries 'in_field' across the recursion. When a w:fldChar with
    type='begin' is encountered, in_field flips to True and stays True (so we
    skip both the field's instruction text AND the cached display value)
    until the matching w:fldChar with type='end'. Without this, SEQ fields
    used for auto-numbering ('[0001]') leak their display value into the
    chunk text and the LLM sees pseudo-IDs glued to real content.
    """
    if state is None:
        state = {"in_field": 0}

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
    if local == 'fldChar':
        # w:fldChar attribute is in the WordprocessingML namespace.
        ftype = el.get(qn('w:fldCharType'))
        if ftype == 'begin':
            state["in_field"] += 1
        elif ftype == 'end' and state["in_field"] > 0:
            state["in_field"] -= 1
        return
    if local == 'instrText':
        # Field instruction text — never visible content.
        return
    if local == 't':
        if el.text and state["in_field"] == 0:
            parts.append(el.text)
        return
    if local == 'br':
        parts.append('\n')
        return

    for child in el:
        _append_translatable_text(child, parts, state)


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


def extract_math_texts(para) -> list[str]:
    """Return visible text from each top-level equation in a paragraph.

    Unlike extract_all_text(), this exposes formula notation so prompts can use
    it as non-output context for assigning parameter legends to equations.
    """
    return [
        text for text in (
            _element_text(el).strip()
            for el in _top_level_math_elements(para)
        )
        if text
    ]


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
# Equation variable extraction
# ---------------------------------------------------------------------------
# Used by the claim writer to redistribute a grouped parameter legend across
# its equations. We extract identifier-like tokens from <m:oMath> XML and
# normalize math-italic Unicode (e.g. 𝛼) to its plain form (α) so symbols in
# the equation match symbols in the legend regardless of styling.

_FUNC_NAMES = {
    "sin", "cos", "tan", "sec", "csc", "cot",
    "sinh", "cosh", "tanh", "log", "ln", "exp",
    "sqrt", "min", "max", "lim", "inf", "sup",
    "arg", "det", "mod", "gcd", "lcm",
}

# Word characters excluding digits and underscore — matches alpha runs in any
# script (Latin, Greek, math-italic …). We then drop any token that is purely
# Hangul or otherwise non-math.
_ALPHA_RUN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_HANGUL_ONLY_RE = re.compile(r"^[가-힯]+$")


def normalize_symbol(s: str) -> str:
    """NFKD-normalize so math-italic chars (𝛼, 𝐴, 𝑎) collapse to plain (α, A, a)."""
    return unicodedata.normalize("NFKD", s)


def extract_equation_variables(omath_el) -> list[str]:
    """Identifier tokens (variable names) inside one <m:oMath> element,
    in first-occurrence order, deduplicated.

    Filters operators, numbers, Hangul labels, and well-known math function
    names (sin, log, …). The returned strings are kept in their original form
    (not normalized) for display purposes; matching against legend symbols
    should use ``normalize_symbol`` on both sides.
    """
    seen: set[str] = set()
    result: list[str] = []
    for el in omath_el.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local != "t" or not el.text:
            continue
        for tok in _ALPHA_RUN_RE.findall(el.text):
            if _HANGUL_ONLY_RE.match(tok):
                continue
            if normalize_symbol(tok).lower() in _FUNC_NAMES:
                continue
            if tok in seen:
                continue
            seen.add(tok)
            result.append(tok)
    return result


def equations_in_paragraph(para) -> list:
    """All top-level <m:oMath> elements inside a paragraph."""
    return _top_level_math_elements(para)


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
#   - the word 'respectively' is either after the value list or before the verb.
# Anything fancier is left alone — better to skip a repair than to mangle text.

# Identifier shape: 1–10 chars, starts with a letter (ASCII or Greek), allows
# digits, subscripts, primes, underscores. Subscript characters U+2080..U+2089.
_SYM = r'[A-Za-zΑ-Ωα-ω][A-Za-z0-9Α-Ωα-ω₀-₉_\']{0,9}'
_VAL = r"[^,;.]+"

_VERB = (
    r'are|denote|denotes|represent|represents|stand\s+for|stands\s+for|'
    r'indicate|indicates|mean|means|refer\s+to|refers\s+to|'
    r'correspond\s+to|corresponds\s+to'
)

_LEGEND_VERB = (
    r'is|are|denote|denotes|represent|represents|stand\s+for|stands\s+for|'
    r'indicate|indicates|mean|means|refer\s+to|refers\s+to|'
    r'correspond\s+to|corresponds\s+to'
)

_RESPECTIVELY_AFTER_VALUES_RE = re.compile(
    rf'(?P<symbols>{_SYM}'
    rf'(?:\s*,\s*{_SYM}){{1,8}}'
    rf'(?:\s*,?\s*and\s+{_SYM})?)'
    rf'\s+(?P<verb>{_VERB})\s+'
    rf'(?P<values>{_VAL}'
    rf'(?:\s*,\s*{_VAL}){{1,8}}'
    rf'(?:\s*,?\s*and\s+{_VAL})?)'
    r'\s*,?\s*respectively',
    flags=re.IGNORECASE,
)

_RESPECTIVELY_BEFORE_VERB_RE = re.compile(
    rf'(?P<symbols>{_SYM}'
    rf'(?:\s*,\s*{_SYM}){{1,8}}'
    rf'(?:\s*,?\s*and\s+{_SYM})?)'
    r'\s+respectively\s+'
    rf'(?P<verb>{_VERB})\s+'
    rf'(?P<values>{_VAL}'
    rf'(?:\s*,\s*{_VAL}){{1,8}}'
    rf'(?:\s*,?\s*and\s+{_VAL})?)',
    flags=re.IGNORECASE,
)

_VERB_SINGULAR = {
    "is": "is",
    "are": "is",
    "denote": "denotes",
    "denotes": "denotes",
    "represent": "represents",
    "represents": "represents",
    "stand for": "stands for",
    "stands for": "stands for",
    "indicate": "indicates",
    "indicates": "indicates",
    "mean": "means",
    "means": "means",
    "refer to": "refers to",
    "refers to": "refers to",
    "correspond to": "corresponds to",
    "corresponds to": "corresponds to",
}

_MALFORMED_SEMICOLON_LEGEND_RE = re.compile(
    rf'(?P<prefix>\bwhere(?:in)?\s+)'
    rf'(?P<symbols>{_SYM}'
    rf'(?:\s*,\s*{_SYM}){{1,8}}'
    rf'(?:\s*,?\s*and\s+{_SYM})?)'
    rf'\s+(?P<verb>{_LEGEND_VERB})\s+'
    rf'(?P<first>{_VAL})'
    rf'(?P<rest>(?:\s*;\s*(?:where(?:in)?\s+)?'
    rf'(?:{_LEGEND_VERB})\s+{_VAL}){{1,8}})',
    flags=re.IGNORECASE,
)

_BARE_LEGEND_CLAUSE_RE = re.compile(
    rf'^\s*;\s*(?:where(?:in)?\s+)?(?P<verb>{_LEGEND_VERB})\s+(?P<value>{_VAL})',
    flags=re.IGNORECASE,
)


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

    text = _RESPECTIVELY_AFTER_VALUES_RE.sub(repl, text)
    return _RESPECTIVELY_BEFORE_VERB_RE.sub(repl, text)


def _repair_malformed_semicolon_legend(text: str) -> str:
    """Repair 'where A, B, and C is X; is Y; is Z' style legends."""

    def repl(m: re.Match) -> str:
        symbols = _split_list(m.group("symbols"))
        values = [m.group("first").strip()]
        verbs = [m.group("verb")]
        for raw_clause in re.findall(r'\s*;\s*(?:where(?:in)?\s+)?(?:' + _LEGEND_VERB + r')\s+[^;.]+', m.group("rest"), flags=re.IGNORECASE):
            clause_match = _BARE_LEGEND_CLAUSE_RE.match(raw_clause)
            if clause_match is None:
                return m.group(0)
            verbs.append(clause_match.group("verb"))
            values.append(clause_match.group("value").strip())

        if len(symbols) != len(values) or len(symbols) < 2:
            return m.group(0)

        clauses: list[str] = []
        for i, (symbol, verb, value) in enumerate(zip(symbols, verbs, values)):
            normalized_verb = re.sub(r'\s+', ' ', verb.lower())
            singular = _VERB_SINGULAR.get(normalized_verb, normalized_verb)
            prefix = m.group("prefix") if i == 0 else ""
            clauses.append(f"{prefix}{symbol} {singular} {value}")
        return "; ".join(clauses)

    return _MALFORMED_SEMICOLON_LEGEND_RE.sub(repl, text)


def postprocess(text: str) -> str:
    text = _normalize_unicode(text)
    text = _expand_respectively(text)
    text = _repair_malformed_semicolon_legend(text)
    text = _break_sentences(text)
    text = _break_after_semicolons(text)
    return text
