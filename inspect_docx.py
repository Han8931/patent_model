"""Terminal viewer for translated .docx files.

Usage:
    uv run inspect_docx.py output/sample_en.docx
    uv run inspect_docx.py output/sample_en.docx --korean-only
    uv run inspect_docx.py output/sample_en.docx --summary
    uv run inspect_docx.py output/sample_en.docx --limit 50
    uv run inspect_docx.py output/sample_en.docx --width 120

Walks every paragraph (including those nested inside tables) and prints:

    NNN  [flags]  paragraph text

Flags:
    KO     paragraph still contains Korean characters (translation gap)
    EQ     paragraph contains a Word equation (<m:oMath>)
    IMG    paragraph contains an inline image (<w:drawing>)
    HDR    detected as a section header (CLAIMS / ABSTRACT / etc.)

`--korean-only` filters to just the paragraphs flagged KO.
`--summary` skips the per-paragraph print and reports totals + ratios.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from docx import Document

from translate.agent.docx_utils import (
    extract_all_text,
    has_drawing,
    has_math,
    iter_all_paragraphs,
)


_HANGUL_RE = re.compile(r"[가-힯]")

_SECTION_HEADER_RE = re.compile(
    r"^\s*(?:"
    r"(?:CLAIMS?|claims?|청구\s*범위|청구항)|"
    r"(?:ABSTRACT|abstract|요\s*약|초\s*록)|"
    r"(?:DETAILED\s*DESCRIPTION|상세\s*설명|발명의\s*내용|발명을\s*실시하기\s*위한\s*구체적인\s*내용)|"
    r"(?:BACKGROUND|background|배경\s*기술)|"
    r"(?:SUMMARY|summary|발명의\s*개요|발명의\s*요약)|"
    r"(?:DRAWINGS?|drawings?|도면의\s*간단한\s*설명)"
    r")\s*$",
    re.IGNORECASE,
)


def _hangul_chars(text: str) -> int:
    return sum(1 for c in text if _HANGUL_RE.fullmatch(c))


def _truncate(s: str, width: int) -> str:
    s = s.replace("\n", " ⏎ ").replace("\t", " ⇥ ")
    if len(s) <= width:
        return s
    return s[: width - 1] + "…"


def _flags(text: str, para) -> list[str]:
    out: list[str] = []
    out.append("KO " if _HANGUL_RE.search(text) else "   ")
    out.append("EQ " if has_math(para) else "   ")
    out.append("IMG" if has_drawing(para) else "   ")
    out.append("HDR" if _SECTION_HEADER_RE.match(text or "") else "   ")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print every paragraph of a .docx, flagging Korean / equation / image content.",
    )
    parser.add_argument("path", type=Path, help="Path to the .docx file to inspect")
    parser.add_argument("--korean-only", action="store_true",
                        help="Only show paragraphs that still contain Korean characters.")
    parser.add_argument("--summary", action="store_true",
                        help="Show totals only (no per-paragraph listing).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N printed paragraphs (0 = no limit).")
    parser.add_argument("--width", type=int, default=140,
                        help="Truncate each paragraph to this many characters (default 140).")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"File not found: {args.path}")
        return 1

    try:
        doc = Document(str(args.path))
    except Exception as exc:
        print(f"Could not open {args.path}: {type(exc).__name__}: {exc}")
        return 1

    total = 0
    blank = 0
    korean_paras = 0
    korean_chars_total = 0
    text_chars_total = 0
    eq_paras = 0
    img_paras = 0
    header_paras = 0

    printed = 0
    for idx, para in enumerate(iter_all_paragraphs(doc)):
        text = extract_all_text(para)
        total += 1
        if not text.strip():
            blank += 1
            continue

        text_chars_total += len(text)
        n_ko = _hangul_chars(text)
        korean_chars_total += n_ko
        is_ko = n_ko > 0
        if is_ko:
            korean_paras += 1
        if has_math(para):
            eq_paras += 1
        if has_drawing(para):
            img_paras += 1
        if _SECTION_HEADER_RE.match(text):
            header_paras += 1

        if args.summary:
            continue
        if args.korean_only and not is_ko:
            continue

        flags = "".join(_flags(text, para))
        snippet = _truncate(text, args.width)
        print(f"{idx:4d}  [{flags}]  {snippet}")
        printed += 1
        if args.limit and printed >= args.limit:
            print(f"... ({args.limit} paragraphs shown; --limit reached)")
            break

    print()
    print("=" * 70)
    print(f"  file               : {args.path}")
    print(f"  total paragraphs   : {total}")
    print(f"  non-blank          : {total - blank}")
    print(f"  section headers    : {header_paras}")
    print(f"  with Word math     : {eq_paras}")
    print(f"  with inline image  : {img_paras}")
    print(f"  contain Korean     : {korean_paras}")
    if text_chars_total:
        ratio = korean_chars_total / text_chars_total * 100.0
        print(f"  Korean / total     : {korean_chars_total}/{text_chars_total} chars ({ratio:.2f}%)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
