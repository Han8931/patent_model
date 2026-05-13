"""Complex math equation sample + pipeline test.

Builds a docx containing several non-trivial OMML structures and runs the
full translation pipeline with a stub LLM. Verifies that:

  1. EVERY <m:oMath> element survives — same XML signature, same source
     paragraph index — via the equation integrity report.
  2. Each text/legend paragraph is translated independently and ends up
     in its OWN <w:p>.
  3. Pure-math paragraphs receive zero LLM calls.

OMML structures exercised:

  * inline subscript  (θ_k, BIT_3k with CJK brackets)
  * inline superscript (x²)
  * inline fraction (a / (b+c))
  * pure-math paragraph holding a multi-element equation
  * mixed paragraph: text + inline math + text + inline math + legend
  * multi-line equation paragraph (eqArr) with three rows that share variables
  * legend paragraph defining symbols

Run:
    uv run python scripts/test_complex_math.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from docx import Document  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402

from translate.translator import PatentTranslator  # noqa: E402
from translate.client import ClientConfig  # noqa: E402


# ---------------------------------------------------------------------------
# OMML element builders (just enough to make structurally-valid math XML that
# python-docx's detection helpers recognize).
# ---------------------------------------------------------------------------

def _m(tag: str):
    return OxmlElement(f"m:{tag}")


def mt(text: str):
    """<m:r><m:t>text</m:t></m:r>"""
    r = _m("r")
    t = _m("t")
    t.text = text
    r.append(t)
    return r


def m_sSub(base_text: str, sub_text: str):
    """Subscript: base_text_sub_text → renders as base_text with subscript."""
    e = _m("sSub")
    base = _m("e")
    base.append(mt(base_text))
    sub = _m("sub")
    sub.append(mt(sub_text))
    e.append(base)
    e.append(sub)
    return e


def m_sSup(base_text: str, sup_text: str):
    e = _m("sSup")
    base = _m("e")
    base.append(mt(base_text))
    sup = _m("sup")
    sup.append(mt(sup_text))
    e.append(base)
    e.append(sup)
    return e


def m_frac(num_text: str, den_text: str):
    e = _m("f")
    num = _m("num")
    num.append(mt(num_text))
    den = _m("den")
    den.append(mt(den_text))
    e.append(num)
    e.append(den)
    return e


def m_eqArr(*rows):
    """Multi-line equation array — each row is a sequence of math children."""
    arr = _m("eqArr")
    for row in rows:
        e = _m("e")
        for child in row:
            e.append(child)
        arr.append(e)
    return arr


def m_oMath(*children):
    """Wrap children in a top-level <m:oMath>."""
    omath = _m("oMath")
    for c in children:
        omath.append(c)
    return omath


def w_t(text: str):
    """Plain text run: <w:r><w:t xml:space='preserve'>text</w:t></w:r>"""
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    r.append(t)
    return r


# ---------------------------------------------------------------------------
# Build the input document
# ---------------------------------------------------------------------------

def make_input(path: Path) -> None:
    doc = Document()
    doc.add_paragraph("[발명의 설명]")
    doc.add_paragraph("[발명의 명칭]")
    doc.add_paragraph("복합 수식 샘플 문서")
    doc.add_paragraph("[발명의 실시를 위한 구체적인 내용]")

    # ── (A) Mixed paragraph: inline subscript + inline superscript + technical text.
    p = doc.add_paragraph()
    p._p.append(w_t("[0001] 본 실시예에서, 입자의 운동 에너지 "))
    p._p.append(m_oMath(m_sSub("E", "k")))
    p._p.append(w_t("는 입자 속도의 제곱 "))
    p._p.append(m_oMath(m_sSup("v", "2")))
    p._p.append(w_t("에 비례하며, 이는 다음 수학식에 의해 나타낼 수 있다."))

    # ── (B) Pure-math paragraph (centered) with a fraction: E_k = (1/2) m v²
    p = doc.add_paragraph()
    p._p.append(m_oMath(
        m_sSub("E", "k"),
        mt(" = "),
        m_frac("1", "2"),
        mt(" m "),
        m_sSup("v", "2"),
    ))
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ── (C) Legend paragraph #1 with detailed multi-clause parameter descriptions.
    doc.add_paragraph(
        "[0002] 여기서, "
        "E_k는 상기 입자의 운동 에너지를 나타내고, 단위는 줄(J)이며, "
        "m은 상기 입자의 질량을 의미하고, 단위는 킬로그램(kg)이며, "
        "v는 상기 입자의 속도를 나타내고, 단위는 미터 매 초(m/s)이다."
    )

    # ── (D) Mixed paragraph: explanatory text + inline equation describing momentum.
    p = doc.add_paragraph()
    p._p.append(w_t("[0003] 또한, 본 실시예에 따른 입자의 운동량 "))
    p._p.append(m_oMath(mt("p")))
    p._p.append(w_t("는 질량 "))
    p._p.append(m_oMath(mt("m")))
    p._p.append(w_t("과 속도 "))
    p._p.append(m_oMath(mt("v")))
    p._p.append(w_t("의 곱으로 정의되며, 이를 다음과 같이 표현할 수 있다."))

    # ── (E) Pure-math paragraph: p = m·v
    p = doc.add_paragraph()
    p._p.append(m_oMath(mt("p = m·v")))
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ── (F) Pure-math paragraph with a multi-row equation array
    #         (3 LLR rows like the patent case the user previously reported).
    p = doc.add_paragraph()
    row1 = [mt("LLR("), m_sSub("〖BIT〗", "3k"), mt(") = -μ·cos("), m_sSub("θ", "k"), mt("+π/8)")]
    row2 = [mt("LLR("), m_sSub("〖BIT〗", "(3k+1)"), mt(") = -μ·sin("), m_sSub("θ", "k"), mt("+π/8)")]
    row3 = [mt("LLR("), m_sSub("〖BIT〗", "(3k+2)"), mt(") = |μ·sin("), m_sSub("θ", "k"), mt("+π/8)| - |μ·cos("), m_sSub("θ", "k"), mt("+π/8)|")]
    p._p.append(m_oMath(m_eqArr(row1, row2, row3)))
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ── (G) Legend paragraph #2 — long, technical, with 7 parameters and varied
    #         clause endings (이고 / 이며 / 이다).
    doc.add_paragraph(
        "[0004] 상기 수학식 1에서, "
        "LLR는 상기 비트 신뢰도 데이터에 대한 로그 가능도 비를 나타내고, "
        "θ_k는 k번째 심볼 구간에 대응하는 위상 차 데이터의 위상 차 값을 의미하며, 단위는 라디안(rad)이고, "
        "k는 0 이상의 정수로서 심볼 인덱스를 나타내며, "
        "〖BIT〗_3k는 상기 k번째 위상에서 가장 상위 비트(MSB)를 의미하고, "
        "〖BIT〗_(3k+1)는 상기 k번째 위상에서 중간 비트를 나타내며, "
        "〖BIT〗_(3k+2)는 상기 k번째 위상에서 가장 하위 비트(LSB)를 의미하고, "
        "μ는 채널 추정에 의해 결정되는 잡음 계수로서, 채널 SNR에 비례하여 결정되는 값이다."
    )

    # ── (H) Mixed paragraph with two inline equations and a long combined legend.
    p = doc.add_paragraph()
    p._p.append(w_t("[0005] 또한, 본 발명의 실시예에 따른 채널 추정기는 "))
    p._p.append(m_oMath(mt("A"), mt(" = "), mt("B"), mt(" + "), mt("C")))
    p._p.append(w_t(" 및 "))
    p._p.append(m_oMath(mt("X"), mt(" = "), mt("Y"), mt(" × "), mt("Z")))
    p._p.append(w_t(
        " 을 만족하도록 구성되고, "
        "여기서 A는 출력 신호의 진폭을 나타내고, "
        "B는 입력 신호의 진폭이며, "
        "C는 채널 이득에 대응하는 보정 계수이고, "
        "X는 출력 채널의 전력 스펙트럼 밀도를 의미하며, "
        "Y는 채널의 응답 함수이고, "
        "Z는 잡음 전력에 대한 정규화 상수이다."
    ))

    # ── (I) Legend-only paragraph using '다만,' as the intro keyword.
    doc.add_paragraph(
        "[0006] 다만, "
        "α는 시스템 이득 계수로서 0 이상 1 이하의 실수이고, "
        "β는 잡음 분산을 나타내며, 단위는 와트(W)이고, "
        "γ는 신호 대 잡음비에 대한 보정 상수이며, "
        "λ는 채널 디코더의 입력 시퀀스 길이이고, "
        "σ는 추정 오차의 표준편차를 의미하며, 단위는 동일하게 적용된다."
    )

    # ── (J) Mixed paragraph with FOUR inline math expressions: a function
    #         definition f(x) = e^x, the derivative f'(x) = e^x, the domain
    #         statement x ∈ ℝ, and the variance σ². Each uses a different
    #         OMML construct (function notation, prime + superscript, set
    #         membership with blackboard-bold style char, lone superscript).
    p = doc.add_paragraph()
    p._p.append(w_t("[0007] 본 실시예에 따른 활성화 함수 "))
    # f(x) = e^x — function notation with exponential superscript.
    p._p.append(m_oMath(
        mt("f(x) = "),
        m_sSup("e", "x"),
    ))
    p._p.append(w_t("의 도함수는 "))
    # f'(x) = e^x — derivative form.
    p._p.append(m_oMath(
        mt("f'(x) = "),
        m_sSup("e", "x"),
    ))
    p._p.append(w_t("이며, 모든 "))
    # x ∈ ℝ — set membership with blackboard-bold ℝ.
    p._p.append(m_oMath(mt("x ∈ ℝ")))
    p._p.append(w_t("에 대해 정의되고, 출력 분포의 분산은 "))
    # σ² — variance (lone superscript).
    p._p.append(m_oMath(m_sSup("σ", "2")))
    p._p.append(w_t("로 표현된다."))

    # ── (K) Pure-math paragraph mixing function/exponential/log structures.
    #         f(x) = e^(σx) + ln(x) + 1/2 · σ²
    p = doc.add_paragraph()
    p._p.append(m_oMath(
        mt("f(x) = "),
        m_sSup("e", "σx"),
        mt(" + ln(x) + "),
        m_frac("1", "2"),
        mt(" · "),
        m_sSup("σ", "2"),
    ))
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ── (L) Legend for the activation function parameters.
    doc.add_paragraph(
        "[0008] 여기서, "
        "f(x)는 본 발명의 실시예에 따른 활성화 함수를 의미하고, 단위는 무차원량이며, "
        "e는 자연 상수로서 약 2.71828의 값을 갖는 무리수이고, "
        "x는 입력 변수로서 임의의 실수 값을 가질 수 있으며, x ∈ ℝ인 관계가 성립하고, "
        "ℤ는 정수의 집합을 나타내며, k ∈ ℤ 의 관계가 성립할 때 인덱스 k는 정수임을 의미하고, "
        "ℝ는 실수의 집합을 나타내고, "
        "σ²는 출력 신호의 분산을 의미하며, σ는 표준편차로서 0 이상의 실수 값을 가지고, "
        "ln(x)는 자연 로그 함수를 나타낸다."
    )

    # ── (M) Mixed paragraph combining set notation and an integral-like expression.
    p = doc.add_paragraph()
    p._p.append(w_t("[0009] 또한, 임의의 정수 "))
    p._p.append(m_oMath(mt("k ∈ ℤ")))
    p._p.append(w_t("에 대해, 출력 시퀀스 "))
    p._p.append(m_oMath(m_sSub("y", "k")))
    p._p.append(w_t("는 입력 시퀀스 "))
    p._p.append(m_oMath(m_sSub("x", "k")))
    p._p.append(w_t("와 가중치 "))
    p._p.append(m_oMath(m_sSub("w", "k")))
    p._p.append(w_t("의 함수로 정의되며, 다음 수학식과 같이 표현된다."))

    # ── (N) Pure-math paragraph: y_k = f(x_k · w_k + σ²)
    p = doc.add_paragraph()
    p._p.append(m_oMath(
        m_sSub("y", "k"),
        mt(" = f("),
        m_sSub("x", "k"),
        mt(" · "),
        m_sSub("w", "k"),
        mt(" + "),
        m_sSup("σ", "2"),
        mt(")"),
    ))
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ── (O) Legend for the output-sequence parameters.
    doc.add_paragraph(
        "[0010] 상기 수학식에서, "
        "y_k는 k번째 시점에서의 출력 시퀀스 값을 나타내고, "
        "x_k는 k번째 시점에서의 입력 시퀀스 값을 의미하며, "
        "w_k는 k번째 가중치로서 학습 과정에서 업데이트되는 파라미터이고, "
        "f(·)는 활성화 함수로서, 본 실시예에서는 f(x) = e^x로 정의되며, "
        "σ²는 잡음 분산을 의미하고, σ는 표준편차이며, "
        "k는 시점 인덱스로서 k ∈ ℤ 의 관계가 성립한다."
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)


# ---------------------------------------------------------------------------
# Stub LLM: returns canned translations matching the Korean content.
# ---------------------------------------------------------------------------

_CANNED = {
    "복합 수식 샘플 문서": "Complex Math Equation Sample Document",
    # ─ Block A (mixed inline math + text segments) ─
    "본 실시예에서, 입자의 운동 에너지":
        "In the present embodiment, the kinetic energy of a particle",
    "는 입자 속도의 제곱":
        "is the square of the particle velocity",
    "에 비례하며, 이는 다음 수학식에 의해 나타낼 수 있다":
        "is proportional to, which can be expressed by the following equation",
    # ─ Block C legend clauses ─
    "E_k는 상기 입자의 운동 에너지를 나타내고, 단위는 줄(J)":
        "E_k denotes the kinetic energy of the particle, with units of joules (J)",
    "m은 상기 입자의 질량을 의미하고, 단위는 킬로그램(kg)":
        "m means the mass of the particle, with units of kilograms (kg)",
    "v는 상기 입자의 속도를 나타내고, 단위는 미터 매 초(m/s)이다":
        "v denotes the velocity of the particle, with units of meters per second (m/s)",
    # ─ Block D (mixed paragraph: momentum definition) ─
    "또한, 본 실시예에 따른 입자의 운동량":
        "In addition, the momentum of the particle according to the present embodiment",
    "는 질량":
        "is the mass",
    "과 속도":
        "and the velocity",
    "의 곱으로 정의되며, 이를 다음과 같이 표현할 수 있다":
        "is defined as the product of, which can be expressed as follows",
    # ─ Block G legend clauses (LLR / phase decoder) ─
    "LLR는 상기 비트 신뢰도 데이터에 대한 로그 가능도 비를 나타내고":
        "LLR denotes the log-likelihood ratio for the bit reliability data",
    "θ_k는 k번째 심볼 구간에 대응하는 위상 차 데이터의 위상 차 값을 의미하며, 단위는 라디안(rad)":
        "θ_k means the phase-difference value of the phase-difference data corresponding to the k-th symbol interval, with units of radians (rad)",
    "k는 0 이상의 정수로서 심볼 인덱스를 나타내며":
        "k is an integer of 0 or more and denotes the symbol index",
    "〖BIT〗_3k는 상기 k번째 위상에서 가장 상위 비트(MSB)를 의미":
        "〖BIT〗_3k means the most significant bit (MSB) at the k-th phase",
    "〖BIT〗_(3k+1)는 상기 k번째 위상에서 중간 비트를 나타내며":
        "〖BIT〗_(3k+1) denotes the middle bit at the k-th phase",
    "〖BIT〗_(3k+2)는 상기 k번째 위상에서 가장 하위 비트(LSB)를 의미":
        "〖BIT〗_(3k+2) means the least significant bit (LSB) at the k-th phase",
    "μ는 채널 추정에 의해 결정되는 잡음 계수로서, 채널 SNR에 비례하여 결정되는 값이다":
        "μ is a noise factor determined by channel estimation, with a value determined in proportion to the channel SNR",
    # ─ Block H (channel estimator inline equations + combined legend) ─
    "또한, 본 발명의 실시예에 따른 채널 추정기":
        "In addition, the channel estimator according to an embodiment of the present invention",
    "및":
        "and",
    "을 만족하도록 구성되고":
        "is configured to satisfy",
    "A는 출력 신호의 진폭을 나타내고":
        "A denotes the amplitude of the output signal",
    "B는 입력 신호의 진폭이며":
        "B is the amplitude of the input signal",
    "C는 채널 이득에 대응하는 보정 계수":
        "C is a correction coefficient corresponding to the channel gain",
    "X는 출력 채널의 전력 스펙트럼 밀도를 의미하며":
        "X means the power spectral density of the output channel",
    "Y는 채널의 응답 함수":
        "Y is the response function of the channel",
    "Z는 잡음 전력에 대한 정규화 상수이다":
        "Z is a normalization constant for the noise power",
    # ─ Block I ('다만,' legend variants) ─
    "α는 시스템 이득 계수로서 0 이상 1 이하의 실수":
        "α is a system gain coefficient, being a real number between 0 and 1 inclusive",
    "β는 잡음 분산을 나타내며, 단위는 와트(W)":
        "β denotes the noise variance, with units of watts (W)",
    "γ는 신호 대 잡음비에 대한 보정 상수이며":
        "γ is a correction constant for the signal-to-noise ratio",
    "λ는 채널 디코더의 입력 시퀀스 길이":
        "λ is the input sequence length of the channel decoder",
    "σ는 추정 오차의 표준편차를 의미하며, 단위는 동일하게 적용된다":
        "σ means the standard deviation of the estimation error, with the same units applied",
    # ─ Block J (mixed paragraph: f(x)=e^x activation + derivative + x∈ℝ + σ²) ─
    "본 실시예에 따른 활성화 함수":
        "the activation function according to the present embodiment",
    "의 도함수":
        "and its derivative",
    "이며, 모든":
        "is, for every",
    "에 대해 정의되고, 출력 분포의 분산":
        "is defined, and the variance of the output distribution",
    "로 표현된다":
        "is expressed as",
    # ─ Block L legend ─
    "f(x)는 본 발명의 실시예에 따른 활성화 함수를 의미하고, 단위는 무차원량이며":
        "f(x) means the activation function according to an embodiment of the present invention, and is a dimensionless quantity",
    "e는 자연 상수로서 약 2.71828의 값을 갖는 무리수":
        "e is the natural constant, an irrational number with a value of approximately 2.71828",
    "x는 입력 변수로서 임의의 실수 값을 가질 수 있으며, x ∈ ℝ인 관계가 성립하고":
        "x is the input variable, capable of taking any real value such that x ∈ ℝ",
    "ℤ는 정수의 집합을 나타내며, k ∈ ℤ 의 관계가 성립할 때 인덱스 k는 정수임을 의미하고":
        "ℤ denotes the set of integers, and the relation k ∈ ℤ indicates that the index k is an integer",
    "ℝ는 실수의 집합을 나타내고":
        "ℝ denotes the set of real numbers",
    "σ²는 출력 신호의 분산을 의미하며, σ는 표준편차로서 0 이상의 실수 값을 가지고":
        "σ² means the variance of the output signal, and σ is the standard deviation, which is a non-negative real value",
    "ln(x)는 자연 로그 함수를 나타낸다":
        "ln(x) denotes the natural logarithm function",
    # ─ Block M (output sequence definition) ─
    "또한, 임의의 정수":
        "In addition, for any integer",
    "에 대해, 출력 시퀀스":
        "the output sequence",
    "는 입력 시퀀스":
        "is the input sequence",
    "와 가중치":
        "and weight",
    "의 함수로 정의되며, 다음 수학식과 같이 표현된다":
        "is defined as a function of, expressed as the following equation",
    # ─ Block O legend ─
    "y_k는 k번째 시점에서의 출력 시퀀스 값을 나타내고":
        "y_k denotes the output-sequence value at the k-th time step",
    "x_k는 k번째 시점에서의 입력 시퀀스 값을 의미하며":
        "x_k means the input-sequence value at the k-th time step",
    "w_k는 k번째 가중치로서 학습 과정에서 업데이트되는 파라미터":
        "w_k is the k-th weight, a parameter updated during training",
    "f(·)는 활성화 함수로서, 본 실시예에서는 f(x) = e^x로 정의되며":
        "f(·) is the activation function, defined in the present embodiment as f(x) = e^x",
    "σ²는 잡음 분산을 의미하고, σ는 표준편차이며":
        "σ² means the noise variance, and σ is the standard deviation",
    "k는 시점 인덱스로서 k ∈ ℤ 의 관계가 성립한다":
        "k is the time-step index, satisfying the relation k ∈ ℤ",
}


def _best_canned(ko: str) -> str:
    """Find the longest canned key that's a substring of ko."""
    best = ""
    for k in _CANNED:
        if k in ko and len(k) > len(best):
            best = k
    return _CANNED.get(best, "(no canned)")


class RecordingStub:
    def __init__(self):
        self.calls: list[str] = []

    def complete(self, messages):
        user = messages[-1]["content"]
        for tag in ("Korean clause:\n", "Korean:\n"):
            if tag in user:
                ko = user.rsplit(tag, 1)[-1].strip()
                break
        else:
            ko = user[:120]
        self.calls.append(ko)
        return json.dumps({"text": _best_canned(ko), "key_terms": []})


# ---------------------------------------------------------------------------
# Test driver
# ---------------------------------------------------------------------------

def main() -> None:
    src = Path("data/sample_complex_math.docx")
    dst = Path("output/sample_complex_math_en.docx")
    make_input(src)
    if dst.exists():
        dst.unlink()

    cfg = ClientConfig(model="stub", base_url="http://x", api_key="x", temperature=0, max_tokens=512)
    translator = PatentTranslator(cfg)
    stub = RecordingStub()
    translator.client = stub

    print(f"--- Translating {src.name} ---")
    translator.translate_document(src, dst, verbose=False, review=False)

    print()
    print(f"=== Stub LLM calls: {len(stub.calls)} ===")
    for i, c in enumerate(stub.calls, 1):
        snippet = c.replace("\n", " ⏎ ")
        print(f"  {i:>2}. {snippet[:80]!r}")

    print()
    print(f"=== {dst.name} structure ===")
    doc = Document(dst)
    for i, p in enumerate(doc.paragraphs):
        seq = []
        for el in p._p.iter():
            tag = el.tag.split("}")[-1]
            if tag == "oMath":
                bits = [c.text for c in el.iter() if c.tag.endswith("}t") and c.text]
                seq.append("<EQ:" + "".join(bits)[:60] + ">")
            elif tag == "t" and el.text and not any(
                a.tag.endswith("}oMath") for a in _ancestors(el)
            ):
                seq.append(repr(el.text)[:80])
        print(f"[{i:>2}] {' | '.join(seq) if seq else '(blank)'}")


def _ancestors(el):
    a = el.getparent()
    while a is not None:
        yield a
        a = a.getparent()


if __name__ == "__main__":
    main()
