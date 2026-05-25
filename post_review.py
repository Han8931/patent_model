"""Post-translation review/fix for an existing translated DOCX.

This is a best-effort standalone stage for when you already have a Korean
source DOCX and an English output DOCX and want to run only the comparison
review without re-translating the whole document.

It pairs Korean-bearing source paragraphs with non-empty English target
paragraphs in order, asks the LLM to compare each pair, and updates only target
paragraphs that need a full corrected translation.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from docx import Document
from dotenv import load_dotenv

from translate.agent.docx_utils import extract_all_text, iter_all_paragraphs, normalize_document_font, replace_text
from translate.agent.glossary import clean_translation_text, extract_json_block
from translate.agent.validation import coverage_problem, translation_problem
from translate.client import ClientConfig, LLMClient


_HANGUL = tuple(chr(c) for c in range(ord("가"), ord("힣") + 1))
_HEADINGS = {
    "DESCRIPTION",
    "TITLE OF INVENTION",
    "BRIEF DESCRIPTION OF THE DRAWINGS",
    "DETAILED DESCRIPTION OF EMBODIMENTS",
    "CLAIMS",
    "ABSTRACT",
    "TECHNICAL PROBLEM",
    "SOLUTION TO PROBLEM",
    "ADVANTAGEOUS EFFECTS OF INVENTION",
    "REPRESENTATIVE FIGURE",
}


def _has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


def _source_items(doc):
    items = []
    for idx, p in enumerate(iter_all_paragraphs(doc)):
        text = extract_all_text(p).strip()
        if text and _has_hangul(text):
            items.append((idx, p, text))
    return items


def _target_items(doc):
    items = []
    for idx, p in enumerate(iter_all_paragraphs(doc)):
        text = (p.text or "").strip()
        if not text:
            continue
        if text.upper() in _HEADINGS:
            continue
        if text.startswith("(") and text.endswith(")") and text[1:-1].isdigit():
            continue
        items.append((idx, p, text))
    return items


def _messages(source: str, target: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are a Korean-to-English patent translation quality reviewer. "
                "Compare the Korean source paragraph with the current English paragraph. "
                "Find omitted words, phrases, sentences, limitations, reference numerals, "
                "pure-letter reference characters such as LD and GC, and equation markers. "
                "If correction is needed, return a complete replacement English paragraph. "
                "Return ONLY valid JSON."
            ),
        },
        {
            "role": "user",
            "content": (
                "If the English is complete and faithful, return:\n"
                '{"needs_revision": false, "issues": [], "revised_text": ""}\n\n'
                "If it omits or mistranslates anything, return:\n"
                '{"needs_revision": true, "issues": ["..."], "revised_text": "complete corrected English paragraph"}\n\n'
                "Keep USPTO patent style. Preserve reference characters and [EQUATION_N] markers.\n\n"
                f"SOURCE_KOREAN:\n{source}\n\nCURRENT_ENGLISH:\n{target}"
            ),
        },
    ]


def _review_pair(client: LLMClient, source: str, target: str) -> tuple[str | None, list[str]]:
    precheck = translation_problem(target) or coverage_problem(source, target)
    messages = _messages(source, target)
    if precheck:
        messages.append({"role": "user", "content": f"Known deterministic issue: {precheck}"})

    last_problem = ""
    for attempt in range(3):
        try:
            raw = client.complete(messages)
            data = extract_json_block(raw)
            if not isinstance(data, dict):
                last_problem = "review response was not valid JSON"
                raise ValueError(last_problem)
            if not data.get("needs_revision") and not precheck:
                return None, []
            revised = clean_translation_text(data.get("revised_text") or data.get("text"))
            if not revised:
                last_problem = "missing revised_text"
                raise ValueError(last_problem)
            problem = translation_problem(revised) or coverage_problem(source, revised)
            if problem:
                last_problem = problem
                raise ValueError(problem)
            issues = data.get("issues") if isinstance(data.get("issues"), list) else []
            return revised, [str(i) for i in issues]
        except Exception as exc:
            if not last_problem:
                last_problem = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                messages.append({
                    "role": "user",
                    "content": (
                        "Try again. Return only valid JSON. revised_text must be a complete "
                        f"corrected English paragraph. Problem: {last_problem}"
                    ),
                })
    return None, [f"review failed: {last_problem}"]


def parse_args():
    load_dotenv()
    env = ClientConfig.from_env()
    p = argparse.ArgumentParser(description="Run only post-translation review/fix on an existing output DOCX.")
    p.add_argument("source", type=Path, help="Original Korean DOCX")
    p.add_argument("translated", type=Path, help="Existing English DOCX to review")
    p.add_argument("output", type=Path, nargs="?", help="Reviewed output DOCX (default: <translated>_reviewed.docx)")
    p.add_argument("--model", default=env.model)
    p.add_argument("--base-url", default=env.base_url)
    p.add_argument("--api-key", default=env.api_key)
    p.add_argument("--temperature", type=float, default=env.temperature)
    p.add_argument("--max-tokens", type=int, default=env.max_tokens)
    p.add_argument("--font", default="Times New Roman")
    p.add_argument("--font-size", type=float, default=12)
    p.add_argument("--audit-only", action="store_true", help="Report issues but do not save revisions")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    output = args.output or args.translated.with_name(f"{args.translated.stem}_reviewed.docx")

    src_doc = Document(args.source)
    dst_doc = Document(args.translated)
    src = _source_items(src_doc)
    dst = _target_items(dst_doc)

    print(f"Source Korean paragraphs: {len(src)}")
    print(f"Target review paragraphs: {len(dst)}")
    if len(src) != len(dst):
        print(
            "WARNING: paragraph counts differ. Pairing is best-effort by order; "
            "for heavily reflowed older outputs, rerunning full translation may be safer."
        )

    client = LLMClient(ClientConfig(
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    ))

    revised = 0
    failed = 0
    for n, ((src_idx, _sp, source), (dst_idx, dp, target)) in enumerate(zip(src, dst), 1):
        fixed, issues = _review_pair(client, source, target)
        if fixed:
            revised += 1
            print(f"[{n}] revised target paragraph {dst_idx}: {'; '.join(issues[:3])}")
            if not args.audit_only:
                replace_text(dp, fixed, args.font)
        elif issues:
            failed += 1
            print(f"[{n}] unresolved target paragraph {dst_idx}: {'; '.join(issues[:3])}")
        if n % 10 == 0 or n == min(len(src), len(dst)):
            print(f"  REVIEW {n}/{min(len(src), len(dst))}")

    if args.audit_only:
        print(f"Audit complete: {revised} paragraph(s) would be revised; {failed} unresolved.")
        return

    normalize_document_font(dst_doc, args.font, args.font_size)
    output.parent.mkdir(parents=True, exist_ok=True)
    dst_doc.save(output)
    print(f"Saved reviewed DOCX → {output} ({revised} revised, {failed} unresolved)")


if __name__ == "__main__":
    main()
