"""Translate a Korean patent .docx into English (USPTO style).

Pipeline:
    1. claims  — translated together so terminology stays consistent;
                 the LLM also returns a Korean→English glossary.
    2. abstract — translated using the glossary.
    3. description — paragraph-by-paragraph, also using the glossary.

Usage:
    uv run python main.py path/to/korean.docx [output.docx]
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import deque
from pathlib import Path
from typing import Callable

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from translate.client import ClientConfig, LLMClient


OUTPUT_FONT = "Times New Roman"

# How many previous (Korean, English) description paragraphs to feed back to the
# LLM as continuity context. Bigger window → smoother antecedent/style flow at
# higher token cost per call. 3 is a reasonable default; bump if descriptions
# rely heavily on multi-paragraph carry-over.
DESCRIPTION_CONTEXT_WINDOW = 3

Progress = Callable[[str], None]


# ---------------------------------------------------------------------------
# Section detection
# ---------------------------------------------------------------------------

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


CLAIM_HEADER_RE = re.compile(r"^[【\[]\s*청구항\s*(\d+)\s*[】\]]\s*")


# Inline images (DrawingML), legacy pictures (VML), and OMML equations live as
# child elements of <w:p>/<w:r>, not as text. We detect them so the pipeline
# can pass those paragraphs through untouched instead of clearing/rewriting them.
_MEDIA_TAGS = (qn("w:drawing"), qn("w:pict"), qn("m:oMath"), qn("m:oMathPara"))


def _has_media(p: Paragraph) -> bool:
    """True if *p* contains an inline image, legacy picture, or OMML equation."""
    el = p._element
    for tag in _MEDIA_TAGS:
        if el.find(f".//{tag}") is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# DOCX paragraph scan
# ---------------------------------------------------------------------------

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
# Prompts
# ---------------------------------------------------------------------------

CLAIMS_PROMPT = """You are a Korean→English patent translator. Translate the
following Korean patent claims into English in USPTO style.

Style rules:
- Each claim begins with its number followed by a period and a space ("1. ", "2. ", ...).
- An independent claim ends "comprising:" and lists its elements separated by semicolons,
  with the final element introduced by " and ".
- A dependent claim begins "The <noun> of claim N, wherein ..." or
  "The <noun> of claim N, further comprising ...".
- If the Korean text says "wherein ... further includes/comprises ...", rewrite it as
  "further comprising".
- Keep terminology consistent across all claims.
- Preserve numerical labels such as "FIG. 1" or "100a".

Output format — plain text with a sentinel, no JSON, no markdown fences:

1. <full English text of claim 1>

2. <full English text of claim 2>

...

---GLOSSARY---
<Korean term> -> <English term>
<Korean term> -> <English term>
...

Rules for the output:
- Start each claim on its own line beginning with "N. " at column 1.
- Separate claims with one blank line.
- Put every Korean→English term pair below the "---GLOSSARY---" sentinel, one per line.
- Include every domain-specific noun, component, or method in the glossary so the
  description translator can stay consistent.
- Output nothing before the first claim and nothing after the last glossary entry."""


CLAIM_RETRY_PROMPT = """You are a Korean→English patent translator. Translate
ONLY the single Korean claim provided into English in USPTO style.

- Start your output with "{N}. " and produce nothing before it.
- Use the same terminology as the previously translated claims provided as context.
- Apply the same USPTO style rules (comprising:, semicolons, "of claim N, wherein/further comprising").
- Output only the English claim text. No preamble, no closing remarks."""


CLAIMS_REVIEW_PROMPT = """You are a USPTO patent attorney reviewing a Korean→English
claims translation. You will receive the Korean source and the current English
translation. Look for:

1. Terminology drift — the same Korean term translated differently across claims.
2. Missing or hallucinated elements relative to the Korean.
3. Wrong USPTO structure — independent claims must end with "comprising:";
   dependents must begin "The <noun> of claim N, wherein ..." or
   "The <noun> of claim N, further comprising ...".
4. Korean text that says "wherein ... further includes/comprises" but the English
   omits "further comprising".

If you find issues, return the corrected FULL set of claims in the same plain-text
format as the input (each claim on its own line beginning with "N. ", claims
separated by one blank line). If there are no issues, return the original English
text unchanged. Output nothing else."""


ABSTRACT_PROMPT = """You are a Korean→English patent translator. Translate the
Korean patent abstract below into English in USPTO style.

- One or two short paragraphs.
- Use the glossary terms verbatim whenever they appear.
- Return only the English text, no preamble or commentary."""


DESCRIPTION_PROMPT = """You are a Korean→English patent translator. Translate
the Korean paragraph below into English in USPTO style.

- Translate faithfully; do not add or omit content.
- Use the glossary terms verbatim whenever they appear.
- If a "Recent context" section is provided, use it to maintain terminology,
  pronoun antecedents, and style continuity with the preceding paragraphs.
  Do NOT retranslate the context — translate ONLY the target paragraph that
  is explicitly marked for translation.
- Return only the English text, no preamble or commentary."""


def _glossary_block(glossary: dict[str, str]) -> str:
    if not glossary:
        return ""
    lines = [f"  {kr}  →  {en}" for kr, en in glossary.items()]
    return "Glossary (use these exact English terms):\n" + "\n".join(lines) + "\n\n"


def _context_block(pairs) -> str:
    """Format the rolling (Korean, English) pairs as 'already translated' context.

    Whitespace inside each paragraph is collapsed to keep the prompt compact;
    the LLM doesn't need the original line layout to use the pair for continuity.
    """
    if not pairs:
        return ""
    lines = [
        "Recent context (already translated; do NOT retranslate — for continuity only):"
    ]
    for kr, en in pairs:
        kr_one = re.sub(r"\s+", " ", kr).strip()
        en_one = re.sub(r"\s+", " ", en).strip()
        lines.append(f"- Korean:  {kr_one}")
        lines.append(f"  English: {en_one}")
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

CLAIMS_MAX_TOKENS = 16384  # bulk claims + review pass — never want to truncate

_GLOSSARY_SENTINEL_RE = re.compile(r"^\s*-{2,}\s*GLOSSARY\s*-{2,}\s*$", re.MULTILINE)
_CLAIM_LINE_RE = re.compile(r"^(\d+)\.\s")
_GLOSSARY_LINE_RE = re.compile(r"^\s*(.+?)\s*(?:->|→|=>|:)\s*(.+?)\s*$")


def _parse_claims_response(raw: str) -> tuple[dict[str, str], dict[str, str]]:
    """Parse a plain-text claims block followed by an optional ---GLOSSARY--- section."""
    text = raw.strip()
    # Strip ``` fences if the model added them anyway.
    fence = re.match(r"```[a-zA-Z]*\s*(.*?)\s*```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    parts = _GLOSSARY_SENTINEL_RE.split(text, maxsplit=1)
    claims_block = parts[0].strip()
    glossary_block = parts[1].strip() if len(parts) > 1 else ""

    claims: dict[str, str] = {}
    current_num: str | None = None
    current_lines: list[str] = []
    for line in claims_block.splitlines():
        m = _CLAIM_LINE_RE.match(line)
        if m:
            if current_num is not None:
                claims[current_num] = "\n".join(current_lines).rstrip()
            current_num = m.group(1)
            current_lines = [line]
        elif current_num is not None:
            current_lines.append(line)
    if current_num is not None:
        claims[current_num] = "\n".join(current_lines).rstrip()

    glossary: dict[str, str] = {}
    for line in glossary_block.splitlines():
        m = _GLOSSARY_LINE_RE.match(line)
        if m:
            glossary[m.group(1).strip()] = m.group(2).strip()

    return claims, glossary


def _claim_korean_by_num(records: list[dict]) -> dict[str, str]:
    """Group claim paragraphs by 【청구항 N】 anchor → joined Korean text."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for r in records:
        if r["role"] != "claim":
            continue
        m = CLAIM_HEADER_RE.match(r["text"])
        if m:
            current = m.group(1)
            out.setdefault(current, []).append(r["text"])
        elif current is not None:
            out.setdefault(current, []).append(r["text"])
    return {k: "\n".join(v) for k, v in out.items()}


def _format_claims_block(claims: dict[str, str], order: list[str]) -> str:
    """Re-emit claims as a plain-text block in the given numeric order."""
    parts = [claims[n] for n in order if n in claims and claims[n].strip()]
    return "\n\n".join(parts)


def _retranslate_single_claim(
    client: LLMClient,
    num: str,
    korean: str,
    prior_english: str,
    glossary: dict[str, str],
) -> str:
    user_parts: list[str] = []
    if glossary:
        user_parts.append(_glossary_block(glossary))
    if prior_english:
        user_parts.append("Previously translated claims (for terminology):")
        user_parts.append(prior_english)
        user_parts.append("")
    user_parts.append(f"Korean claim {num}:")
    user_parts.append(korean)
    return client.complete(
        [
            {"role": "system", "content": CLAIM_RETRY_PROMPT.format(N=num)},
            {"role": "user",   "content": "\n".join(user_parts)},
        ],
        max_tokens=CLAIMS_MAX_TOKENS,
    ).strip()


def _review_claims(
    client: LLMClient,
    korean_block: str,
    english_block: str,
    glossary: dict[str, str],
) -> dict[str, str]:
    """Run a review pass; return any corrected claims (may be empty)."""
    user_parts: list[str] = []
    if glossary:
        user_parts.append(_glossary_block(glossary))
    user_parts.append("Korean claims:")
    user_parts.append(korean_block)
    user_parts.append("")
    user_parts.append("Current English translation:")
    user_parts.append(english_block)
    raw = client.complete(
        [
            {"role": "system", "content": CLAIMS_REVIEW_PROMPT},
            {"role": "user",   "content": "\n".join(user_parts)},
        ],
        max_tokens=CLAIMS_MAX_TOKENS,
    )
    revised, _ = _parse_claims_response(raw)
    return revised


def translate_claims(
    client: LLMClient,
    korean_block: str,
    records: list[dict],
    *,
    progress: Progress = print,
    logger: logging.Logger | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (claim_num → English claim text, glossary).

    Strategy:
      1. Bulk-translate all claims in one call (best for terminology consistency).
      2. Validate: every 【청구항 N】 anchor in the source must have a non-empty
         English claim. Retry missing ones individually with the rest as context.
      3. Run a review pass on the assembled English block; apply any corrections.
    """
    # --- 1) bulk translate ---------------------------------------------------
    raw = client.complete(
        [
            {"role": "system", "content": CLAIMS_PROMPT},
            {"role": "user",   "content": korean_block},
        ],
        max_tokens=CLAIMS_MAX_TOKENS,
    )
    claims, glossary = _parse_claims_response(raw)

    # --- 2) validate against the Korean anchors, retry missing ---------------
    korean_by_num = _claim_korean_by_num(records)
    expected = sorted(korean_by_num.keys(), key=int) if korean_by_num else \
               sorted(claims.keys(), key=lambda s: int(s))

    missing = [n for n in expected if not claims.get(n, "").strip()]
    if missing:
        progress(f"      bulk pass missed {len(missing)} claim(s) {missing}; retrying individually…")
        if logger:
            logger.warning(f"bulk claims pass missed {len(missing)} claim(s): {missing}")
        prior = _format_claims_block(claims, [n for n in expected if n not in missing])
        for n in missing:
            kr = korean_by_num.get(n)
            if not kr:
                continue
            try:
                claims[n] = _retranslate_single_claim(client, n, kr, prior, glossary)
            except Exception as e:
                progress(f"      retry of claim {n} failed: {e!r}")
                if logger:
                    logger.exception(f"retry of claim {n} failed")
                continue
            # extend prior context so each retry sees the previous retries too
            prior = (prior + "\n\n" + claims[n]).strip()

    # --- 3) review pass ------------------------------------------------------
    english_block = _format_claims_block(claims, expected)
    if english_block:
        progress("      running review pass…")
        try:
            revised = _review_claims(client, korean_block, english_block, glossary)
        except Exception as e:
            progress(f"      review failed — skipping: {e!r}")
            if logger:
                logger.exception("review pass failed")
            revised = {}
        applied = 0
        for n, text in revised.items():
            if n in expected and text.strip() and text.strip() != claims.get(n, "").strip():
                claims[n] = text
                applied += 1
        if applied:
            progress(f"      review revised {applied} claim(s)")
            if logger:
                logger.info(f"review revised {applied} claim(s)")
        else:
            progress("      review found no changes")

    return claims, glossary


def translate_abstract(client: LLMClient, korean: str, glossary: dict[str, str]) -> str:
    user = _glossary_block(glossary) + "Korean abstract:\n" + korean
    return client.complete([
        {"role": "system", "content": ABSTRACT_PROMPT},
        {"role": "user",   "content": user},
    ]).strip()


def translate_paragraph(
    client: LLMClient,
    korean: str,
    glossary: dict[str, str],
    *,
    context=None,
) -> str:
    """Translate one description paragraph.

    *context* is an iterable of ``(korean, english)`` pairs from the immediately
    preceding paragraphs. The LLM sees them as read-only continuity, not as
    targets to retranslate. Pass ``None`` (or an empty iterable) on the first
    paragraph; the loop in :func:`translate_file` grows the window from there.
    """
    parts: list[str] = []
    if glossary:
        parts.append(_glossary_block(glossary))
    if context:
        parts.append(_context_block(context))
    parts.append("Korean paragraph to translate:\n" + korean)
    return client.complete([
        {"role": "system", "content": DESCRIPTION_PROMPT},
        {"role": "user",   "content": "".join(parts)},
    ]).strip()


# ---------------------------------------------------------------------------
# DOCX write-back
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
    for r in p.runs:
        r.text = ""


def _apply_output_font(doc) -> None:
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
# Apply translations
# ---------------------------------------------------------------------------

def _format_claim_uspto(text: str) -> str:
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

    Each claim is reformatted via :func:`_format_claim_uspto` before being
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
        all_text = "\n\n".join(_format_claim_uspto(claims[k]) for k in ordered)
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
        set_paragraph_text(doc.paragraphs[idx], _format_claim_uspto(text))
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
    for r in records:
        if r["role"] == "header" and r.get("section"):
            set_paragraph_text(doc.paragraphs[r["index"]], f"[{r['section']}]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_logger(in_path: Path, log_path: Path) -> logging.Logger:
    """Per-file logger that writes to *log_path* and is independent of other calls.

    We instantiate ``Logger`` directly (instead of ``getLogger``) so concurrent
    translations in batch mode don't share handlers via the global registry.
    """
    logger = logging.Logger(f"translate.{in_path.stem}")
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    return logger


def _teardown_logger(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def resolve_output_path(in_path: Path, output: Path | None, suffix: str) -> Path:
    """Decide where to write the English .docx given the CLI args.

    Default: ``output/<stem>_en.docx``. If *suffix* is non-empty it's appended
    to the stem (so ``--suffix _v1`` → ``output/<stem>_en_v1.docx``). Suffix
    is also applied when *output* is given, so explicit names track versions
    the same way.
    """
    out_path = output if output else Path("output") / f"{in_path.stem}_en.docx"
    if suffix:
        out_path = out_path.with_stem(out_path.stem + suffix)
    return out_path


def translate_file(
    in_path: Path,
    out_path: Path,
    *,
    progress: Progress = print,
    model: str | None = None,
) -> None:
    """Translate one .docx end-to-end. Used by both the CLI and ``batch.py``.

    Errors are logged to ``<out_path>.log`` (overwriting per run). Failures in
    a single description paragraph leave the Korean text in place rather than
    aborting the whole file; the same is true for total claims or abstract
    failures — they're logged and the pipeline continues.

    *model* overrides ``LLM_MODEL`` from ``.env`` for this run only; the rest
    of the LLM config (base URL, API key, temperature, max_tokens) still comes
    from the environment.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = out_path.with_suffix(".log")
    logger = _setup_logger(in_path, log_path)
    logger.info(f"translate {in_path} -> {out_path}")

    try:
        config = ClientConfig.from_env()
        if model:
            config.model = model
        logger.info(f"model={config.model} base_url={config.base_url}")
        client = LLMClient(config)
        doc = Document(in_path)
        records = scan_paragraphs(doc)

        media = [r for r in records if r["role"] == "media"]
        mixed_media = [r for r in media if r["text"].strip()]
        if media:
            logger.info(
                f"{len(media)} paragraph(s) contain inline images/equations; "
                f"passing through untouched"
            )
        if mixed_media:
            progress(
                f"      NOTE: {len(mixed_media)} paragraph(s) mix text with "
                f"images/equations; left untranslated (see {log_path.name})"
            )
            logger.warning(
                f"{len(mixed_media)} paragraph(s) have Korean text alongside "
                f"inline media; not translated to keep media intact"
            )
            for r in mixed_media:
                snippet = re.sub(r"\s+", " ", r["text"])[:120]
                logger.warning(f"  mixed-media paragraph idx={r['index']}: '{snippet}…'")

        # ----- 1) claims (single LLM call, all claims at once) -----
        claims_block = collect_block(records, "claim")
        claims_en: dict[str, str] = {}
        glossary: dict[str, str] = {}
        if claims_block:
            n_paras = sum(1 for r in records if r["role"] == "claim")
            progress(f"[1/3] Translating claims ({n_paras} paragraphs, single LLM call)…")
            try:
                claims_en, glossary = translate_claims(
                    client, claims_block, records, progress=progress, logger=logger
                )
                progress(f"      got {len(claims_en)} claim(s); glossary={len(glossary)} entries")
                logger.info(f"claims translated: {len(claims_en)}; glossary entries: {len(glossary)}")
            except Exception as e:
                progress(f"      ERROR: claims translation failed — {e!r}")
                logger.exception("claims translation failed entirely")
        else:
            progress("[1/3] No claims section found — skipping.")
            logger.info("no claims section")

        # ----- 2) abstract -----
        abstract_block = collect_block(records, "abstract")
        abstract_en = ""
        if abstract_block:
            progress("[2/3] Translating abstract…")
            try:
                abstract_en = translate_abstract(client, abstract_block, glossary)
            except Exception as e:
                progress(f"      ERROR: abstract translation failed — {e!r}")
                logger.exception("abstract translation failed")
        else:
            progress("[2/3] No abstract section found — skipping.")
            logger.info("no abstract section")

        # ----- 3) description, paragraph-by-paragraph with rolling context -----
        desc_records = [r for r in records if r["role"] == "description" and r["text"].strip()]
        progress(
            f"[3/3] Translating description ({len(desc_records)} paragraphs, "
            f"context window={DESCRIPTION_CONTEXT_WINDOW})…"
        )
        total = len(desc_records)
        failures = 0
        # Only successful (korean, english) pairs feed back as context — a failed
        # paragraph stays in the source as Korean and is omitted from context so
        # it can't pollute the next call's terminology.
        context: deque = deque(maxlen=DESCRIPTION_CONTEXT_WINDOW)
        for n, r in enumerate(desc_records, 1):
            try:
                translation = translate_paragraph(
                    client, r["text"], glossary, context=context
                )
                r["translation"] = translation
                context.append((r["text"], translation))
            except Exception as e:
                failures += 1
                snippet = r["text"][:160].replace("\n", " ")
                logger.error(
                    f"description paragraph idx={r['index']} failed: {e!r}; korean='{snippet}…'"
                )
            if n % 10 == 0 or n == total:
                progress(f"      {n}/{total}")
        if failures:
            progress(f"      WARNING: {failures} paragraph(s) failed; Korean left in place (see {log_path.name})")
            logger.warning(f"{failures} description paragraph(s) failed to translate")

        # ----- write back -----
        apply_section_headers(doc, records)
        apply_claims(doc, records, claims_en, progress=progress)
        apply_abstract(doc, records, abstract_en)
        for r in desc_records:
            translation = r.get("translation")
            if translation:
                set_paragraph_text(doc.paragraphs[r["index"]], translation)
            # else: paragraph failed — Korean stays so reviewer sees it

        _apply_output_font(doc)

        doc.save(out_path)
        progress(f"wrote {out_path}")
        logger.info(f"wrote {out_path}")
    except Exception:
        logger.exception("translate_file failed")
        raise
    finally:
        _teardown_logger(logger)


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate a Korean patent .docx to English.")
    parser.add_argument("path", type=Path, help="Input .docx file")
    parser.add_argument("output", type=Path, nargs="?", default=None,
                        help="Output .docx (default: output/<stem>_en.docx)")
    parser.add_argument("--suffix", default="",
                        help="Suffix inserted before .docx (e.g. --suffix _v1 → ..._en_v1.docx)")
    parser.add_argument("--model", default=None,
                        help="Override LLM_MODEL from .env (e.g. --model qwen3.5)")
    args = parser.parse_args()

    if not args.path.is_file():
        sys.exit(f"input not found: {args.path}")

    out_path = resolve_output_path(args.path, args.output, args.suffix)
    translate_file(args.path, out_path, model=args.model)


if __name__ == "__main__":
    main()
