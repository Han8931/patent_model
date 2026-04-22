"""Paragraph-level Korean→English patent translator using an OpenAI-compatible API."""

import re
import shutil
import time
from collections import deque
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from .client import LLMClient, ClientConfig
from .prompt import Prompt, SECTION_PROMPTS, DEFAULT_PROMPT

# XML namespaces
_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
_XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'

# Section headers translated by lookup — never sent to the LLM
SECTION_HEADER_MAP = {
    "[발명의 설명]":                    "DESCRIPTION",
    "[발명의 명칭]":                    "TITLE OF INVENTION",
    "[도면의 간단한 설명]":              "BRIEF DESCRIPTION OF THE DRAWINGS",
    "[발명의 실시를 위한 구체적인 내용]": "DETAILED DESCRIPTION OF EMBODIMENTS",
    "[특허청구범위]":                   "CLAIMS",
    "[청구범위]":                       "CLAIMS",       # alternate form used in this doc
    "[요약서]":                         "ABSTRACT",
    "[요약]":                           "ABSTRACT",     # sub-heading that appears after [요약서]
    "[발명의 효과]":                    "ADVANTAGEOUS EFFECTS OF INVENTION",
    "[기술적 과제]":                    "TECHNICAL PROBLEM",
    "[과제의 해결 수단]":               "SOLUTION TO PROBLEM",
}

# Korean claim header: 【청구항 N】 or [청구항 N] (with optional spaces)
_CLAIM_HEADER_RE = re.compile(r'^[【\[]\s*청구항\s*(\d+)\s*[】\]]\s*$')

# Any leading claim-number prefix the LLM might produce: "1.", "1:", "CLAIM 1.", "Claim 1 "
_LLM_CLAIM_PREFIX_RE = re.compile(r'^(?:CLAIM\s+)?(\d+)[.:\s]\s*', re.IGNORECASE)

_BLANK_RE = re.compile(r'^\s*$')

_ABBREVS = {
    "fig", "figs", "e.g", "i.e", "no", "nos", "u.s", "vol", "approx",
    "cf", "vs", "et al", "sec", "art", "para", "ref", "dept",
}


# ---------------------------------------------------------------------------
# Text post-processing
# ---------------------------------------------------------------------------

# Characters that Times New Roman (and most Latin fonts) lack glyphs for,
# mapped to their safe ASCII equivalents.
_UNICODE_NORMALIZE_MAP = str.maketrans({
    '‑': '-',   # non-breaking hyphen → hyphen-minus
    '‐': '-',   # hyphen → hyphen-minus
    '‒': '-',   # figure dash → hyphen-minus
    '–': '-',   # en dash → hyphen-minus (patent style prefers hyphen)
    '—': '-',   # em dash → hyphen-minus
    ' ': ' ',   # non-breaking space → regular space
    ' ': ' ',   # narrow no-break space → regular space
    ' ': ' ',   # thin space → regular space
    '‘': "'",   # left single quotation mark → apostrophe
    '’': "'",   # right single quotation mark → apostrophe
    '“': '"',   # left double quotation mark → straight quote
    '”': '"',   # right double quotation mark → straight quote
    '…': '...',  # ellipsis → three dots
})


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


def _normalize_claim(text: str, claim_num: int) -> str:
    """Strip any LLM-produced prefix and prepend the canonical 'N. ' form."""
    text = _LLM_CLAIM_PREFIX_RE.sub('', text.strip())
    return f'{claim_num}. {text}'


# ---------------------------------------------------------------------------
# docx helpers
# ---------------------------------------------------------------------------

def _has_non_text_content(para) -> bool:
    p = para._p
    return (
        p.find('.//{%s}drawing' % _W) is not None or
        p.find('.//{%s}oMath' % _M) is not None
    )


def _text_runs(para) -> list:
    return [r for r in para.runs if r.text]


def _write_run_with_breaks(run, text: str, font_name: str) -> None:
    """
    Replace run content with text, converting \\n to <w:br/> elements so
    line breaks actually render in Word.
    """
    r = run._r
    # Remove existing w:t and w:br children from this run
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


def _replace_text(para, new_text: str, font_name: str) -> None:
    """
    Write new_text into the paragraph's text runs, leaving non-text XML
    (drawings, equations) untouched. \\n becomes a proper Word line break.
    """
    runs = _text_runs(para)
    if not runs:
        return
    _write_run_with_breaks(runs[0], new_text, font_name)
    for run in runs[1:]:
        run.text = ''
        run.font.name = font_name
    for run in para.runs:
        run.font.name = font_name


def _word_count(text: str) -> int:
    return len(text.split())


def _insert_para_after(ref_para, text: str, font_name: str) -> None:
    """Insert a plain paragraph immediately after ref_para."""
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


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------

class PatentTranslator:
    def __init__(self, config: ClientConfig | None = None, context_window: int = 3):
        self.config = config or ClientConfig()
        self.client = LLMClient(self.config)
        self.context_window = context_window

    def _translate_text(
        self,
        text: str,
        context: list[tuple[str, str]],
        prompt: Prompt,
    ) -> str:
        messages = prompt.build_messages(text, context=context)
        result = self.client.complete(messages).strip()
        return postprocess(result)

    def translate_document(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        font: str = "Times New Roman",
        delay: float = 0.5,
        verbose: bool = True,
    ) -> None:
        input_path = Path(input_path)
        output_path = Path(output_path)

        shutil.copy2(input_path, output_path)
        doc = Document(output_path)

        history: deque[tuple[str, str]] = deque(maxlen=self.context_window)

        current_section: str | None = None
        current_prompt: Prompt = DEFAULT_PROMPT
        pending_claim_num: int | None = None   # set when 【청구항 N】 header is seen
        abstract_last_para = None
        abstract_texts: list[str] = []

        def _flush_abstract_word_count() -> None:
            if abstract_last_para is not None and abstract_texts:
                count = _word_count(' '.join(abstract_texts))
                _insert_para_after(abstract_last_para, f'({count})', font)
                if verbose:
                    print(f"      ABSTRACT word count → ({count})")

        for i, para in enumerate(doc.paragraphs):
            raw = para.text

            if _BLANK_RE.match(raw):
                continue

            if _has_non_text_content(para):
                for run in para.runs:
                    run.font.name = font
                if verbose:
                    print(f"[{i:03d}] PRESERVED (image/equation)")
                continue

            # --- Section header ---
            mapped = SECTION_HEADER_MAP.get(raw.strip())
            if mapped:
                # Leaving ABSTRACT: flush word count before switching section
                if current_section == "ABSTRACT" and mapped != "ABSTRACT":
                    _flush_abstract_word_count()
                    abstract_last_para = None
                    abstract_texts = []
                # Entering a new real section (ignore duplicate ABSTRACT sub-headers)
                if mapped != current_section:
                    current_section = mapped
                    current_prompt = SECTION_PROMPTS.get(mapped, DEFAULT_PROMPT)
                    pending_claim_num = None
                _replace_text(para, mapped, font)
                if verbose:
                    print(f"[{i:03d}] HEADER → {mapped}  [prompt: {current_prompt.name}]")
                continue

            # --- Claim number header: 【청구항 N】 ---
            claim_match = _CLAIM_HEADER_RE.match(raw.strip())
            if claim_match and current_section == "CLAIMS":
                pending_claim_num = int(claim_match.group(1))
                # Blank out the Korean marker — number will be prepended to claim body
                _replace_text(para, '', font)
                if verbose:
                    print(f"[{i:03d}] CLAIM HEADER → pending #{pending_claim_num}")
                continue

            # --- Regular paragraph: translate ---
            if verbose:
                preview = raw[:60].replace('\n', ' ')
                print(f"[{i:03d}] Translating: {preview}…")

            translated = self._translate_text(raw, list(history), current_prompt)

            # Normalize claim body: strip any LLM prefix, prepend canonical N.
            if current_section == "CLAIMS" and pending_claim_num is not None:
                translated = _normalize_claim(translated, pending_claim_num)
                pending_claim_num = None

            history.append((raw, translated))
            _replace_text(para, translated, font)

            if current_section == "ABSTRACT":
                abstract_last_para = para
                abstract_texts.append(translated)

            if delay > 0:
                time.sleep(delay)

        _flush_abstract_word_count()

        doc.save(output_path)
        if verbose:
            print(f"\nSaved → {output_path}")
