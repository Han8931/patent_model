"""End-to-end test for image-based equations (PNG/JPG inserted as <w:drawing>).

Builds a docx with TWO equation shapes:

  Block A — pure-image equation paragraph (PNG centered on its own line).
            Korean intro + equation-image + Korean legend in 3 paragraphs.

  Block B — MIXED paragraph: text + inline image-equation + text + a second
            inline image-equation + text. This is the failure mode the user
            reported: the LLM doesn't see a marker for the image, so the
            writer can't preserve its position.

Stub LLM records calls. We verify:
  * 'Equation integrity: OK' — drawings present pre-translation are still
    there post-translation (the integrity report also counts <w:oMath>, so
    image-only docs report 0 tracked and pass trivially).
  * Block A's standalone image stays in its own <w:p> at its source index.
  * Block B's two inline images keep their relative source position, with
    translated text interleaved between them.

Run:
    uv run python scripts/test_image_equations.py
"""

from __future__ import annotations

import io
import json
import struct
import sys
import zlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from docx import Document  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.shared import Inches  # noqa: E402

from translate.translator import PatentTranslator  # noqa: E402
from translate.client import ClientConfig  # noqa: E402


def _minimal_png_bytes(width: int = 120, height: int = 32) -> bytes:
    """Build a tiny solid-grey PNG without depending on Pillow.

    Just enough bytes to be a valid PNG so python-docx's add_picture accepts it.
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit grayscale
    # 1 row = 1 filter byte + width bytes of gray data
    row = b"\x00" + (b"\xC0" * width)
    raw = row * height
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _add_inline_image(p, image_bytes: bytes, width: float = 1.2):
    """Append an inline <w:drawing> picture to paragraph ``p`` (no own run)."""
    # python-docx's add_run().add_picture appends a new run with the drawing.
    p.add_run().add_picture(io.BytesIO(image_bytes), width=Inches(width))


def _txt(text: str):
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    r.append(t)
    return r


def make_input(path: Path) -> None:
    doc = Document()
    doc.add_paragraph("[발명의 설명]")
    doc.add_paragraph("[발명의 명칭]")
    doc.add_paragraph("이미지 수식 샘플 문서")
    doc.add_paragraph("[발명의 실시를 위한 구체적인 내용]")

    img_bytes = _minimal_png_bytes()

    # ── Block A — pure-image equation between text paragraphs.
    doc.add_paragraph("[0001] 본 실시예에서는 아래와 같은 수학식을 사용한다.")
    p = doc.add_paragraph()
    _add_inline_image(p, img_bytes, width=1.5)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph(
        "[0002] 여기서, A는 출력 신호의 진폭이고, B는 입력 신호의 진폭이며, C는 채널 이득이다."
    )

    # ── Block B — MIXED paragraph: text + 2 inline image-equations + text + legend.
    p = doc.add_paragraph()
    p._p.append(_txt("[0003] 또한, 본 실시예는 다음 식 "))
    p.add_run().add_picture(io.BytesIO(img_bytes), width=Inches(1.0))
    p._p.append(_txt(" 및 "))
    p.add_run().add_picture(io.BytesIO(img_bytes), width=Inches(1.0))
    p._p.append(_txt(
        " 을 만족하고, 여기서 X는 출력의 분산이고, Y는 입력의 분산이며, Z는 잡음 계수이다."
    ))

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)


_CANNED = {
    "이미지 수식 샘플 문서": "Image Equation Sample Document",
    # Block A
    "본 실시예에서는 아래와 같은 수학식을 사용한다": "The present embodiment uses the following equation",
    "A는 출력 신호의 진폭": "A is the amplitude of the output signal",
    "B는 입력 신호의 진폭": "B is the amplitude of the input signal",
    "C는 채널 이득이다": "C is the channel gain",
    # Block B
    "또한, 본 실시예는 다음 식": "In addition, the present embodiment satisfies the following equation",
    "및": "and",
    "을 만족하고": "is satisfied,",
    "X는 출력의 분산": "X is the variance of the output",
    "Y는 입력의 분산": "Y is the variance of the input",
    "Z는 잡음 계수이다": "Z is the noise factor",
}


def _best_canned(ko: str) -> str:
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


def _ancestors(el):
    a = el.getparent()
    while a is not None:
        yield a
        a = a.getparent()


def _count_drawings(p) -> int:
    return sum(
        1 for el in p._p.iter()
        if el.tag.endswith("}drawing")
    )


def main() -> None:
    src = Path("data/sample_image_equations.docx")
    dst = Path("output/sample_image_equations_en.docx")
    make_input(src)
    if dst.exists():
        dst.unlink()

    src_doc = Document(src)
    src_drawing_counts = [_count_drawings(p) for p in src_doc.paragraphs]
    print(f"SOURCE drawing count per paragraph: {src_drawing_counts}")

    cfg = ClientConfig(model="stub", base_url="http://x", api_key="x", temperature=0, max_tokens=256)
    t = PatentTranslator(cfg)
    stub = RecordingStub()
    t.client = stub
    print()
    print(f"--- Translating {src.name} ---")
    t.translate_document(src, dst, verbose=False, review=False)

    print()
    print(f"=== Stub LLM calls: {len(stub.calls)} ===")
    for i, c in enumerate(stub.calls, 1):
        print(f"  {i:>2}. {c[:80]!r}")

    print()
    print(f"=== {dst.name} structure ===")
    out_doc = Document(dst)
    out_drawing_counts = [_count_drawings(p) for p in out_doc.paragraphs]
    print(f"OUTPUT drawing count per paragraph: {out_drawing_counts}")
    print(f"Source drawings total: {sum(src_drawing_counts)}; output: {sum(out_drawing_counts)}")
    print()
    for i, p in enumerate(out_doc.paragraphs):
        seq = []
        for el in p._p.iter():
            tag = el.tag.split("}")[-1]
            if tag == "drawing":
                seq.append("<IMG>")
            elif tag == "t" and el.text and not any(
                a.tag.endswith("}oMath") for a in _ancestors(el)
            ):
                seq.append(repr(el.text)[:80])
        print(f"[{i:>2}] {' | '.join(seq) if seq else '(blank)'}")


if __name__ == "__main__":
    main()
