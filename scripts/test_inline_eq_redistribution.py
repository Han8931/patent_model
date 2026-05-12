"""Verify inline multi-equation paragraphs survive LLM grouping.

Builds a single paragraph with text + 3 inline <m:oMath> elements + a legend.
The stub LLM emits a 'grouped' translation where every [EQUATION_N] marker
sits together at the start and all descriptions are bunched at the end —
the exact failure mode the user reported.

Variable-aware redistribution should rescue this, placing each description
right after the equation whose variables it actually uses.

Run:
    uv run python scripts/test_inline_eq_redistribution.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from docx import Document  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402

from translate.agent.docx_utils import replace_text  # noqa: E402


def _math_run(text: str):
    r = OxmlElement("m:r")
    t = OxmlElement("m:t")
    t.text = text
    r.append(t)
    return r


def _omath(*tokens: str):
    omath = OxmlElement("m:oMath")
    for tok in tokens:
        omath.append(_math_run(tok))
    return omath


def build_para_with_three_inline_eqs(doc):
    """One paragraph containing: text + EQ(A=B+C) + text + EQ(X=Y×Z) + text + EQ(M=N-P) + text."""
    p = doc.add_paragraph()
    # Initial text
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "다음 식을 만족한다: "
    r.append(t)
    p._p.append(r)

    p._p.append(_omath("A", " = ", "B", " + ", "C"))

    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = ", "
    r.append(t)
    p._p.append(r)

    p._p.append(_omath("X", " = ", "Y", " × ", "Z"))

    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = ", "
    r.append(t)
    p._p.append(r)

    p._p.append(_omath("M", " = ", "N", " - ", "P"))

    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = " 여기서, A는 두께, B는 폭, C는 높이, X는 길이, Y는 면적, Z는 밀도, M은 질량, N은 부피, P는 압력이다."
    r.append(t)
    p._p.append(r)
    return p


def render(p) -> str:
    """Linear rendering of paragraph showing where text and math sit."""
    out = []
    for el in p._p.iter():
        tag = el.tag.split("}")[-1]
        if tag == "oMath":
            # Collect equation variables for visibility.
            vars_ = [c.text for c in el.iter() if c.tag.endswith("}t") and c.text]
            out.append("<EQ:" + "".join(vars_) + ">")
        elif tag == "t" and el.text and not any(p.tag.endswith("}oMath") for p in _ancestors(el)):
            out.append(el.text)
    return "".join(out)


def _ancestors(el):
    a = el.getparent()
    while a is not None:
        yield a
        a = a.getparent()


def main() -> None:
    # Simulated LLM output: GROUPED markers (the user's symptom).
    grouped_translation = (
        "The following equations are satisfied: [EQUATION_1] [EQUATION_2] [EQUATION_3] "
        "where A is a thickness, B is a width, C is a height, "
        "X is a length, Y is an area, Z is a density, "
        "M is a mass, N is a volume, P is a pressure."
    )

    doc = Document()
    p = build_para_with_three_inline_eqs(doc)

    print("BEFORE:")
    print("  " + render(p))

    replace_text(p, grouped_translation, "Times New Roman")

    print("\nAFTER (expect each EQ followed by its OWN clauses):")
    print("  " + render(p))


if __name__ == "__main__":
    main()
