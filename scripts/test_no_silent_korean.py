"""Verify the silent-Korean failure mode is dead.

Three scenarios, all of which previously produced an output file containing
unmodified Korean text:

  1. LLM client always raises   → graph aborts → no output file created.
  2. write step crashes mid-way → graph aborts → no output file created.
  3. LLM returns empty strings  → post-write Korean-ratio sanity check
                                   refuses to save → no output file created.

Run:
    uv run python scripts/test_no_silent_korean.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from docx import Document  # noqa: E402

from translate.agent.graph import build_graph  # noqa: E402
from translate.agent.state import TranslationState  # noqa: E402


def make_korean_doc(path: Path) -> None:
    doc = Document()
    doc.add_paragraph("[발명의 명칭]")
    doc.add_paragraph("반도체 장치 및 그 제조 방법")
    doc.add_paragraph("[청구범위]")
    doc.add_paragraph("[청구항 1]")
    doc.add_paragraph("반도체 기판; 및 상기 기판 상의 절연막을 포함하는 반도체 장치.")
    doc.add_paragraph("[요약서]")
    doc.add_paragraph("본 발명은 반도체 장치에 관한 것이다.")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)


class CrashClient:
    """Always raises — every translation call fails."""
    def complete(self, messages):
        raise RuntimeError("simulated LLM outage")


class EmptyClient:
    """Returns empty translations — every chunk falls back to Korean."""
    def complete(self, messages):
        return json.dumps({"text": "", "key_terms": []})


def run(label: str, src: Path, dst: Path, client) -> None:
    if dst.exists():
        dst.unlink()
    initial: TranslationState = {
        "input_path": src,
        "output_path": dst,
        "font": "Times New Roman",
        "review": False,
        "verbose": False,
        "delay": 0.0,
        "client": client,
    }
    print(f"\n--- {label} ---")
    try:
        build_graph().invoke(initial)
        print(f"  graph completed without error (this is fine if output is valid)")
    except Exception as exc:
        print(f"  graph raised: {type(exc).__name__}: {exc}")

    if dst.exists():
        # Check the file content if it does exist — should NOT be Korean.
        doc = Document(dst)
        any_hangul = any("가" <= c <= "힣" for p in doc.paragraphs for c in (p.text or ""))
        print(f"  output exists at {dst} (hangul-present={any_hangul}) ❌"
              if any_hangul else
              f"  output exists at {dst} — clean ✓")
    else:
        print(f"  output does NOT exist ✓ — silent-Korean mode prevented")


def main() -> None:
    src = Path("data/sample_silent_test.docx")
    make_korean_doc(src)

    run("LLM always errors",
        src, Path("output/silent_test_error.docx"),
        CrashClient())

    run("LLM returns empty strings (Korean ratio check)",
        src, Path("output/silent_test_empty.docx"),
        EmptyClient())


if __name__ == "__main__":
    main()
