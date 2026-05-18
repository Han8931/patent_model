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
import re
import sys
from pathlib import Path

from docx import Document
from docx.text.paragraph import Paragraph

from translate.client import ClientConfig, LLMClient


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


# ---------------------------------------------------------------------------
# DOCX paragraph scan
# ---------------------------------------------------------------------------

def scan_paragraphs(doc) -> list[dict]:
    """Walk the document and tag each paragraph with its section + role.

    role ∈ {"header", "claim", "abstract", "description", "blank"}
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
- Return only the English text, no preamble or commentary."""


def _glossary_block(glossary: dict[str, str]) -> str:
    if not glossary:
        return ""
    lines = [f"  {kr}  →  {en}" for kr, en in glossary.items()]
    return "Glossary (use these exact English terms):\n" + "\n".join(lines) + "\n\n"


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
        print(f"      bulk pass missed {len(missing)} claim(s) {missing}; retrying individually…")
        prior = _format_claims_block(claims, [n for n in expected if n not in missing])
        for n in missing:
            kr = korean_by_num.get(n)
            if not kr:
                continue
            claims[n] = _retranslate_single_claim(client, n, kr, prior, glossary)
            # extend prior context so each retry sees the previous retries too
            prior = (prior + "\n\n" + claims[n]).strip()

    # --- 3) review pass ------------------------------------------------------
    english_block = _format_claims_block(claims, expected)
    if english_block:
        print("      running review pass…")
        revised = _review_claims(client, korean_block, english_block, glossary)
        applied = 0
        for n, text in revised.items():
            if n in expected and text.strip() and text.strip() != claims.get(n, "").strip():
                claims[n] = text
                applied += 1
        if applied:
            print(f"      review revised {applied} claim(s)")
        else:
            print("      review found no changes")

    return claims, glossary


def translate_abstract(client: LLMClient, korean: str, glossary: dict[str, str]) -> str:
    user = _glossary_block(glossary) + "Korean abstract:\n" + korean
    return client.complete([
        {"role": "system", "content": ABSTRACT_PROMPT},
        {"role": "user",   "content": user},
    ]).strip()


def translate_paragraph(client: LLMClient, korean: str, glossary: dict[str, str]) -> str:
    user = _glossary_block(glossary) + "Korean paragraph:\n" + korean
    return client.complete([
        {"role": "system", "content": DESCRIPTION_PROMPT},
        {"role": "user",   "content": user},
    ]).strip()


# ---------------------------------------------------------------------------
# DOCX write-back
# ---------------------------------------------------------------------------

def set_paragraph_text(p: Paragraph, new_text: str) -> None:
    """Replace a paragraph's text in place; embedded "\\n" become soft breaks."""
    lines = (new_text or "").split("\n")

    if not p.runs:
        run = p.add_run(lines[0])
        for line in lines[1:]:
            run.add_break()
            p.add_run(line)
        return

    first = p.runs[0]
    first.text = lines[0]
    for r in p.runs[1:]:
        r.text = ""
    for line in lines[1:]:
        first.add_break()
        p.add_run(line)


def clear_paragraph(p: Paragraph) -> None:
    for r in p.runs:
        r.text = ""


# ---------------------------------------------------------------------------
# Apply translations
# ---------------------------------------------------------------------------

def apply_claims(doc, records: list[dict], claims: dict[str, str]) -> None:
    """Place each English claim onto the paragraph that held its 【청구항 N】 header.

    Other paragraphs in the CLAIMS section are cleared so the original Korean
    element-list paragraphs don't bleed through under the English claim.
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
        all_text = "\n\n".join(claims[k] for k in sorted(claims, key=lambda s: int(s)))
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
        set_paragraph_text(doc.paragraphs[idx], text)
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
        print(f"      WARNING: {len(untranslated)} claim(s) without translation "
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

def main() -> None:
    parser = argparse.ArgumentParser(description="Translate a Korean patent .docx to English.")
    parser.add_argument("path", type=Path, help="Input .docx file")
    parser.add_argument("output", type=Path, nargs="?", default=None,
                        help="Output .docx (default: output/<stem>_en.docx)")
    args = parser.parse_args()

    in_path: Path = args.path
    if not in_path.is_file():
        sys.exit(f"input not found: {in_path}")

    out_path: Path = args.output or Path("output") / f"{in_path.stem}_en.docx"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    client = LLMClient(ClientConfig.from_env())

    doc = Document(in_path)
    records = scan_paragraphs(doc)

    # ----- 1) claims (single LLM call, all claims at once) -----
    claims_block = collect_block(records, "claim")
    if claims_block:
        n_paras = sum(1 for r in records if r["role"] == "claim")
        print(f"[1/3] Translating claims ({n_paras} paragraphs, single LLM call)…")
        claims_en, glossary = translate_claims(client, claims_block, records)
        print(f"      got {len(claims_en)} claim(s); glossary={len(glossary)} entries")
    else:
        print("[1/3] No claims section found — skipping.")
        claims_en, glossary = {}, {}

    # ----- 2) abstract -----
    abstract_block = collect_block(records, "abstract")
    if abstract_block:
        print("[2/3] Translating abstract…")
        abstract_en = translate_abstract(client, abstract_block, glossary)
    else:
        print("[2/3] No abstract section found — skipping.")
        abstract_en = ""

    # ----- 3) description, paragraph-by-paragraph -----
    desc_records = [r for r in records if r["role"] == "description" and r["text"].strip()]
    print(f"[3/3] Translating description ({len(desc_records)} paragraphs)…")
    for n, r in enumerate(desc_records, 1):
        r["translation"] = translate_paragraph(client, r["text"], glossary)
        print(f"      {n}/{len(desc_records)}")

    # ----- write back -----
    apply_section_headers(doc, records)
    apply_claims(doc, records, claims_en)
    apply_abstract(doc, records, abstract_en)
    for r in desc_records:
        set_paragraph_text(doc.paragraphs[r["index"]], r.get("translation", ""))

    doc.save(out_path)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
