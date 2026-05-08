"""End-to-end offline test for the 'N grouped equations + combined legend' fix.

Builds an in-memory docx with one claim shaped like:
    [claim header]
    [intro text] 다음 식들을 만족한다:
    [eq1]  (Word OMML)
    [eq2]  (Word OMML)
    [eq3]  (Word OMML)
    [legend text] 여기서, A는 ..., B는 ..., X는 ..., Y는 ..., M은 ..., N은 ...

Runs the agent graph with a stub LLM that returns interleaved output:
    intro + [EQUATION_1] desc1 + [EQUATION_2] desc2 + [EQUATION_3] desc3
and verifies the rendered docx places each description IMMEDIATELY after
its own equation paragraph (not at the legend, not all bunched at the end).

Run:
    uv run python scripts/test_grouped_eq_layout.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from docx import Document  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402

from translate.agent.graph import build_graph  # noqa: E402
from translate.agent.state import TranslationState  # noqa: E402


def _math_run(text: str):
    r = OxmlElement("m:r")
    t = OxmlElement("m:t")
    t.text = text
    r.append(t)
    return r


def _add_equation_paragraph(doc, *tokens: str):
    """Add a paragraph that contains only one inline <m:oMath> equation."""
    p = doc.add_paragraph()
    omath = OxmlElement("m:oMath")
    for tok in tokens:
        omath.append(_math_run(tok))
    p._p.append(omath)
    return p


def make_input_docx(path: Path) -> None:
    doc = Document()
    doc.add_paragraph("[청구범위]")
    doc.add_paragraph("[청구항 1]")
    doc.add_paragraph(
        "반도체 장치로서, 다음 식들을 만족하는 반도체 장치:"
    )
    _add_equation_paragraph(doc, "A", " = ", "B", " + ", "C")
    _add_equation_paragraph(doc, "X", " = ", "Y", " × ", "Z")
    _add_equation_paragraph(doc, "M", " = ", "N", " - ", "P")
    doc.add_paragraph(
        "여기서, A는 두께이고, B는 폭이고, C는 높이이고, "
        "X는 길이이고, Y는 면적이고, Z는 밀도이고, "
        "M은 질량이고, N은 부피이고, P는 압력이다."
    )
    doc.save(path)


# Stub returns the LLM-translation we hope a well-instructed model produces:
# interleaved equation + per-equation legend.
GOOD_CLAIM = (
    "A semiconductor device satisfying:\n"
    "[EQUATION_1], where A is a thickness, B is a width, and C is a height;\n"
    "[EQUATION_2], where X is a length, Y is an area, and Z is a density; and\n"
    "[EQUATION_3], where M is a mass, N is a volume, and P is a pressure."
)


class StubClient:
    def complete(self, messages):
        # Print the user prompt so we can verify the GROUPED-EQUATION
        # LAYOUT REPAIR instruction was actually injected.
        user = messages[1]["content"]
        if "GROUPED-EQUATION LAYOUT REPAIR" in user:
            print("[stub] user prompt contains layout-repair instruction ✓")
        else:
            print("[stub] user prompt MISSING layout-repair instruction ✗")
        return json.dumps({"text": GOOD_CLAIM, "key_terms": []})


def inspect(path: Path) -> None:
    doc = Document(path)
    print(f"\n=== Output paragraphs of {path.name} ===")
    for i, p in enumerate(doc.paragraphs):
        seq = []
        for el in p._p.iter():
            tag = el.tag.split("}")[-1]
            if tag == "oMath":
                seq.append("<EQ>")
            elif tag == "t" and el.text:
                seq.append(repr(el.text))
        print(f"[{i}] {' | '.join(seq) if seq else '(blank)'}")


def main() -> None:
    src = Path("data/sample_grouped_eq.docx")
    dst = Path("output/sample_grouped_eq_en.docx")
    src.parent.mkdir(parents=True, exist_ok=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    make_input_docx(src)

    if dst.exists():
        dst.unlink()

    initial: TranslationState = {
        "input_path": src,
        "output_path": dst,
        "font": "Times New Roman",
        "review": False,
        "verbose": False,
        "delay": 0.0,
        "client": StubClient(),
    }
    build_graph().invoke(initial)
    inspect(dst)


if __name__ == "__main__":
    main()
