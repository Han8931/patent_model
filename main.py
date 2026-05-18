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
import json
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

Return STRICT JSON with this exact shape and nothing else:
{
  "claims": {
    "1": "1. <full English text of claim 1>",
    "2": "2. <full English text of claim 2>",
    ...
  },
  "glossary": {"<Korean term>": "<English term>", ...}
}

The glossary should list every domain-specific noun, component, or method that
appears in the claims so the description translator can stay consistent."""


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

def _parse_json(raw: str) -> dict:
    """Parse JSON from an LLM response, tolerating ```fences``` and surrounding prose."""
    text = raw.strip()
    fence = re.match(r"```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def translate_claims(client: LLMClient, korean: str) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (claim_num → English claim text, glossary)."""
    raw = client.complete([
        {"role": "system", "content": CLAIMS_PROMPT},
        {"role": "user",   "content": korean},
    ])
    data = _parse_json(raw)
    claims = {str(k): str(v).strip() for k, v in (data.get("claims") or {}).items()}
    glossary = {str(k): str(v) for k, v in (data.get("glossary") or {}).items()}
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
    for num, idx in anchors.items():
        text = claims.get(num)
        if text is None:
            continue
        set_paragraph_text(doc.paragraphs[idx], text)
        written.add(idx)

    for r in claim_records:
        if r["index"] not in written:
            clear_paragraph(doc.paragraphs[r["index"]])


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
        claims_en, glossary = translate_claims(client, claims_block)
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
