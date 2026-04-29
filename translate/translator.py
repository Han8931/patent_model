"""Paragraph-level Korean→English patent translator using an OpenAI-compatible API."""

import json
import re
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Callable

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from .client import LLMClient, ClientConfig
from .prompt import (Prompt, SECTION_PROMPTS, DEFAULT_PROMPT,
                      build_batch_messages, build_decision_messages, build_revision_messages)

def _extract_json(text: str):
    """Parse JSON from LLM output that may contain surrounding prose."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
    return None


def _parse_batch_response(text: str, count: int) -> list[str | None]:
    """Parse [N] numbered translation output back into an ordered list.

    Splits on [N] markers; content between consecutive markers belongs to
    the preceding index. Returns None for any index not found in the output.
    """
    results: list[str | None] = [None] * count
    # re.split with a capturing group keeps the captured index in the list
    parts = re.split(r'\n?\[(\d+)\]\s*', text.strip())
    # parts = [pre-text, idx, content, idx, content, ...]
    i = 1
    while i + 1 < len(parts):
        try:
            idx = int(parts[i])
            content = parts[i + 1].strip()
            if 0 <= idx < count and content:
                results[idx] = postprocess(content)
        except (ValueError, IndexError):
            pass
        i += 2
    return results


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
    "[발명의 효과]":                    "ADVANTAGEOUS EFFECTS OF INVENTION",
    "[기술적 과제]":                    "TECHNICAL PROBLEM",
    "[과제의 해결 수단]":               "SOLUTION TO PROBLEM",
    "[대표도]":                         "REPRESENTATIVE FIGURE",
    "대표도":                           "REPRESENTATIVE FIGURE",
}

# Korean claim header: 【청구항 N】 or [청구항 N] (with optional spaces).
# No $ — also matches when body text follows on the same paragraph.
_CLAIM_HEADER_RE = re.compile(r'^[【\[]\s*청구항\s*(\d+)\s*[】\]]\s*')

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
# Input normalization
# ---------------------------------------------------------------------------

# Heading style name prefixes (Word built-in + common Korean equivalents)
_HEADING_STYLE_PREFIXES = ('heading', '제목', '표제')

# Standalone paragraphs to blank unconditionally before translation.
# [요약] ("summary") is a redundant sub-heading that appears both inside the
# [요약서] abstract section and sometimes in the description body — blanking it
# everywhere is safe because [요약서] alone is sufficient to trigger ABSTRACT mode.
_UNCONDITIONAL_BLANK = {"[요약]"}

# Regex matching any known section-header key at the start of a paragraph,
# followed by whitespace and more content (i.e. it is a prefix, not the whole text).
_SECTION_PREFIX_RE = re.compile(
    r'^(?:' + '|'.join(
        re.escape(k) for k in sorted(SECTION_HEADER_MAP, key=len, reverse=True)
    ) + r')\s+'
)


def _normalize_document(doc, font: str) -> None:
    """Pre-translation normalization of the copied document.

    1. Blank unconditionally problematic sub-headings (e.g. [요약]).
    2. Reset heading paragraph styles → Normal, eliminating Word outline/fold
       formatting that causes section labels to appear in multiple places.
    3. Strip embedded section-label prefixes from content paragraphs, e.g.
       "[발명의 명칭] 반도체 패키지를..." → "반도체 패키지를..."
    """
    for para in doc.paragraphs:
        text = para.text.strip()

        # Blank known redundant sub-headings regardless of position
        if text in _UNCONDITIONAL_BLANK:
            _replace_text(para, '', font)
            continue

        # Reset heading styles to Normal
        if para.style and any(
            para.style.name.lower().startswith(p) for p in _HEADING_STYLE_PREFIXES
        ):
            try:
                para.style = doc.styles['Normal']
            except KeyError:
                pass

        # Strip embedded section-label prefix when paragraph has content after it
        m = _SECTION_PREFIX_RE.match(para.text)
        if m and m.end() < len(para.text):
            _replace_text(para, para.text[m.end():], font)


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------

class PatentTranslator:
    def __init__(
        self,
        config: ClientConfig | None = None,
        batch_size: int = 30,
    ):
        self.config = config or ClientConfig()
        self.client = LLMClient(self.config)
        self.batch_size = batch_size

    def _translate_batch(
        self,
        section: str,
        items: list[str],
        prompt: Prompt,
        verbose: bool = False,
    ) -> list[str | None]:
        """Translate a list of paragraphs in one LLM call. Returns None per item on failure."""
        if not items:
            return []
        messages = build_batch_messages(section, prompt, items)
        try:
            raw = self.client.complete(messages)
            results = _parse_batch_response(raw, len(items))
            missing = sum(1 for r in results if r is None)
            if missing and verbose:
                print(f"  Warning: {missing}/{len(items)} paragraphs failed to parse — will fall back")
            return results
        except Exception as exc:
            if verbose:
                print(f"  Batch call failed: {exc}")
            return [None] * len(items)

    def _translate_single(self, text: str, prompt: Prompt) -> str:
        """Fallback: translate one paragraph independently."""
        messages = prompt.build_messages(text)
        try:
            return postprocess(self.client.complete(messages).strip())
        except Exception:
            return text

    def _decide_revision(
        self,
        section: str,
        pairs: list[tuple[str, str]],
        verbose: bool,
    ) -> dict:
        try:
            messages = build_decision_messages(section, pairs)
            raw = self.client.complete(messages)
            result = _extract_json(raw)
            if isinstance(result, dict):
                return result
        except Exception as exc:
            if verbose:
                print(f"  [REVIEW] Decision call failed: {exc}")
        return {"needs_revision": False, "issues": []}

    def _revise_section(
        self,
        section: str,
        pairs: list[tuple[str, str]],
        issues: list[str],
        verbose: bool,
    ) -> list[dict]:
        try:
            messages = build_revision_messages(section, pairs, issues)
            raw = self.client.complete(messages)
            result = _extract_json(raw)
            if isinstance(result, list):
                return result
        except Exception as exc:
            if verbose:
                print(f"  [REVIEW] Revision call failed: {exc}")
        return []

    def _review_section(
        self,
        section_name: str,
        buffer: list[tuple],   # (para, korean, translated)
        font: str,
        verbose: bool,
    ) -> None:
        if len(buffer) < 2:
            return

        pairs = [(kr, en) for _, kr, en in buffer]

        if verbose:
            print(f"\n  [REVIEW] Checking {section_name} ({len(pairs)} paragraphs)…")

        decision = self._decide_revision(section_name, pairs, verbose)
        if not decision.get("needs_revision", False):
            if verbose:
                print(f"  [REVIEW] {section_name} — no revision needed.")
            return

        issues = decision.get("issues", [])
        if verbose:
            for issue in issues:
                print(f"  [REVIEW] Issue: {issue}")

        revisions = self._revise_section(section_name, pairs, issues, verbose)

        applied = 0
        for item in revisions:
            idx = item.get("index")
            text = item.get("text")
            if idx is not None and isinstance(text, str) and 0 <= idx < len(buffer):
                para, _, _ = buffer[idx]
                _replace_text(para, postprocess(text), font)
                applied += 1

        if verbose:
            print(f"  [REVIEW] Applied {applied} revision(s).")

    def translate_document(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        font: str = "Times New Roman",
        delay: float = 0.5,
        verbose: bool = True,
        review: bool = True,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        start_time = time.time()
        input_path = Path(input_path)
        output_path = Path(output_path)

        shutil.copy2(input_path, output_path)
        doc = Document(output_path)

        def _progress(msg: str) -> None:
            if progress_callback:
                progress_callback(msg)

        # ------------------------------------------------------------------
        # Pass 1: classify every paragraph
        # ------------------------------------------------------------------
        records: list[dict] = []
        current_section: str | None = None

        for para in doc.paragraphs:
            raw = para.text
            stripped = raw.strip()

            if _BLANK_RE.match(raw):
                records.append({"kind": "blank", "para": para, "section": current_section})
                continue

            if _has_non_text_content(para) and not _text_runs(para):
                records.append({"kind": "image", "para": para, "section": current_section})
                continue

            mapped = SECTION_HEADER_MAP.get(stripped)
            if mapped:
                if mapped != current_section:
                    current_section = mapped
                records.append({"kind": "section_header", "para": para, "raw": raw,
                                 "section": current_section, "mapped": mapped})
                continue

            m = _CLAIM_HEADER_RE.match(stripped)
            if m and current_section == "CLAIMS":
                claim_num = int(m.group(1))
                body = stripped[m.end():]
                if not body:
                    # Standalone header paragraph: write "N." directly in Pass 2
                    records.append({"kind": "claim_header", "para": para, "raw": raw,
                                     "section": current_section, "claim_num": claim_num})
                else:
                    # Header + body in one paragraph: translate body, then prepend "N. "
                    records.append({"kind": "text", "para": para, "raw": body,
                                     "section": current_section,
                                     "mixed": _has_non_text_content(para),
                                     "claim_num": claim_num})
                continue

            records.append({"kind": "text", "para": para, "raw": raw,
                             "section": current_section,
                             "mixed": _has_non_text_content(para)})

        # ------------------------------------------------------------------
        # Pass 2: apply non-translation transformations immediately
        # ------------------------------------------------------------------
        for r in records:
            if r["kind"] == "section_header":
                _replace_text(r["para"], r["mapped"], font)
                _progress(f"→ {r['mapped']}")
                if verbose:
                    print(f"HEADER → {r['mapped']}")
            elif r["kind"] == "claim_header":
                _replace_text(r["para"], f"{r['claim_num']}.", font)
            elif r["kind"] == "image":
                for run in r["para"].runs:
                    run.font.name = font
                if verbose:
                    print("PRESERVED (image/equation)")

        # ------------------------------------------------------------------
        # Pass 3: batch-translate text records, flushing at section boundaries
        # ------------------------------------------------------------------
        total_text = sum(1 for r in records if r["kind"] == "text")
        translated_count = 0
        abstract_records: list[dict] = []
        review_buffers: dict[str, list[tuple]] = {}

        pending: list[dict] = []
        current_batch_section: str | None = None

        def flush() -> None:
            nonlocal translated_count
            if not pending:
                return

            section = pending[0]["section"] or "BODY"
            prompt = SECTION_PROMPTS.get(section, DEFAULT_PROMPT)
            items = [r["raw"] for r in pending]

            _progress(f"Translating {section} ({len(items)} paragraphs)…")
            if verbose:
                print(f"\nTranslating {section} ({len(items)} paragraphs)…")

            translations = self._translate_batch(section, items, prompt, verbose)

            for r, t in zip(pending, translations):
                if t is None:
                    if verbose:
                        print(f"  Fallback: {r['raw'][:60]}…")
                    t = self._translate_single(r["raw"], prompt)

                if section == "CLAIMS":
                    t = _LLM_CLAIM_PREFIX_RE.sub('', t.strip())
                    if r.get("claim_num") is not None:
                        t = f"{r['claim_num']}. {t}"

                _replace_text(r["para"], t, font)
                r["translation"] = t

                if section == "ABSTRACT":
                    abstract_records.append(r)

                review_buffers.setdefault(section, []).append(
                    (r["para"], r["raw"], t)
                )

                translated_count += 1

            _progress(f"{translated_count}/{total_text} paragraphs")
            pending.clear()

            if delay > 0:
                time.sleep(delay)

        for r in records:
            if r["kind"] != "text":
                if r["kind"] == "section_header" and r["mapped"] != current_batch_section:
                    flush()
                    current_batch_section = r["mapped"]
                continue

            section = r["section"]
            if section != current_batch_section and pending:
                flush()
            current_batch_section = section
            pending.append(r)

            if len(pending) >= self.batch_size:
                flush()

        flush()

        # ------------------------------------------------------------------
        # Abstract word count
        # ------------------------------------------------------------------
        if abstract_records:
            count = _word_count(
                ' '.join(r["translation"] for r in abstract_records if "translation" in r)
            )
            _insert_para_after(abstract_records[-1]["para"], f'({count})', font)
            if verbose:
                print(f"\nABSTRACT word count → ({count})")

        # ------------------------------------------------------------------
        # Review pass (per section)
        # ------------------------------------------------------------------
        if review:
            for section_name, buffer in review_buffers.items():
                if len(buffer) >= 2:
                    _progress(f"Reviewing {section_name}…")
                    self._review_section(section_name, buffer, font, verbose)
                    _progress("Review done")

        # ------------------------------------------------------------------
        # Save
        # ------------------------------------------------------------------
        doc.save(output_path)
        elapsed = time.time() - start_time
        minutes, seconds = divmod(int(elapsed), 60)
        elapsed_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
        _progress(f"Done in {elapsed_str} → {output_path}")
        if verbose:
            print(f"\nSaved → {output_path}")
            print(f"Total time: {elapsed_str}")
