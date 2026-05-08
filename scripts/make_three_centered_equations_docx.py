"""Generate a Korean sample .docx with three centered equation paragraphs.

Shape:
    intro text
    centered equation 1
    centered equation 2
    centered equation 3
    combined Korean parameter legend

Run:
    uv run python scripts/make_three_centered_equations_docx.py
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement


def _math_run(text: str) -> "OxmlElement":
    r = OxmlElement("m:r")
    t = OxmlElement("m:t")
    t.text = text
    r.append(t)
    return r


def _add_centered_equation(doc: Document, *tokens: str) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    omath = OxmlElement("m:oMath")
    for token in tokens:
        omath.append(_math_run(token))
    p._p.append(omath)


def main() -> None:
    out = (
        Path(__file__).resolve().parent.parent
        / "data"
        / "sample_three_centered_equations.docx"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    doc.add_paragraph("본 발명은 다음 세 식을 만족하는 반도체 장치에 관한 것이다:")

    _add_centered_equation(doc, "S", "₁", " = ", "α", "X", " + ", "β")
    _add_centered_equation(doc, "S", "₂", " = ", "β", "Y", " + ", "γ")
    _add_centered_equation(doc, "S", "₃", " = ", "γ", "Z", " + ", "α")

    doc.add_paragraph(
        "여기서, α는 제1 가중치이고, β는 제2 가중치이고, "
        "γ는 보정 계수이고, X는 제1 입력값이고, Y는 제2 입력값이고, "
        "Z는 제3 입력값이다."
    )

    doc.add_paragraph(
        "상기 파라미터들은 공정 조건에 따라 독립적으로 조정될 수 있다."
    )

    doc.save(out)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
