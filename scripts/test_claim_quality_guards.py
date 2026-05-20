"""Offline checks for deterministic claim quality guards.

Run:
    uv run python scripts/test_claim_quality_guards.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from translate.agent.nodes.chunk_claims import chunk_claims  # noqa: E402
from translate.agent.nodes.translate_claims import translate_claims  # noqa: E402
from translate.agent.state import ParagraphRecord  # noqa: E402


class StubClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        user = messages[1]["content"]
        if "Previous bulk translation was unusable" in user:
            return (
                "2. The semiconductor device of claim 1, wherein the "
                "semiconductor device further comprises an encapsulant "
                "(inner sealing member)."
            )
        return (
            "===== CLAIM 1 =====\n"
            "A semiconductor device comprising: a substrate; and a gate electrode.\n\n"
            "===== CLAIM 2 =====\n"
            "The semiconductor device of claim 1, wherein the encapsulant further comprises resin.\n\n"
            "===== CLAIM 3 =====\n"
            "The semiconductor device of claim 1, wherein the substrate comprises silicon.\n\n"
            "===== GLOSSARY =====\n"
            "반도체 장치 → semiconductor device\n"
        )


def _record(index: int, raw: str, *, kind: str = "text", claim_num: int | None = None):
    return ParagraphRecord(
        index=index,
        kind=kind,
        para=None,
        raw=raw,
        section="CLAIMS",
        mapped="CLAIMS" if kind == "section_header" else None,
        claim_num=claim_num,
    )


def main() -> None:
    state = {
        "records": [
            _record(0, "[청구범위]", kind="section_header"),
            _record(1, "[청구항 1]", kind="claim_header", claim_num=1),
            _record(2, "기판; 및 게이트 전극을 포함하는 반도체 장치.", claim_num=1),
            _record(3, "[청구항 2]", kind="claim_header", claim_num=2),
            _record(
                4,
                "청구항 1에 있어서, 상기 반도체 장치는 봉지재(내부 실링 부재)를 더 포함하는 반도체 장치.",
                claim_num=2,
            ),
            _record(5, "[청구항 3]", kind="claim_header", claim_num=3),
            _record(
                6,
                "청구항 1에 있어서, 상기 기판은 실리콘을 포함하는 반도체 장치.",
                claim_num=3,
            ),
        ]
    }
    state.update(chunk_claims(state))
    stub = StubClient()
    state["client"] = stub
    state["progress"] = lambda _: None
    translate_claims(state)

    by_num = {c.claim_num: c.translation for c in state["chunks_claims"]}
    assert by_num[2] == (
        "2. The semiconductor device of claim 1, further comprising "
        "an encapsulant (inner sealing member)."
    )
    assert by_num[3] == (
        "3. The semiconductor device of claim 1, wherein the substrate comprises silicon."
    )
    assert stub.calls == 2
    print("claim quality guards: ok")


if __name__ == "__main__":
    main()
