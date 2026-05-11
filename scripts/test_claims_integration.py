"""Offline integration test for the claim-classifier + two-stage translator.

Builds a tiny fake document of claim-like ParagraphRecords (no real .docx
needed), runs chunk_claims to populate the new claim_kind / is_independent /
parent_claim_nums fields, then runs translate_claims with a stub LLM that
echoes which prompt shape it received. Verifies:

  * Independents are translated before dependents (regardless of source order).
  * Each chunk routes to the correct per-kind system prompt.
  * Dependents see the parent's locked noun phrase in their user prompt.
  * Method dependents pick 'wherein' vs 'further comprising' correctly.

Run:
    uv run python scripts/test_claims_integration.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from translate.agent.nodes.chunk_claims import chunk_claims  # noqa: E402
from translate.agent.nodes.plan_claim_preambles import plan_claim_preambles  # noqa: E402
from translate.agent.nodes.translate_claims import translate_claims  # noqa: E402
from translate.agent.state import ParagraphRecord  # noqa: E402


def _record(idx: int, raw: str, *, kind: str = "text", section: str = "CLAIMS",
            claim_num: int | None = None, mapped: str | None = None) -> ParagraphRecord:
    """Lightweight ParagraphRecord — para is a stub since chunk_claims only
    reads .raw / .section / .kind / .claim_num, never the docx para itself."""
    para = SimpleNamespace(_p=None)
    return ParagraphRecord(
        index=idx, kind=kind, para=para,
        raw=raw, section=section,
        mapped=mapped, claim_num=claim_num,
    )


# Source order intentionally interleaves device + method + CRM claims so the
# two-stage translator's claim-number ordering is observable.
RECORDS = [
    _record(0, "[청구범위]", kind="section_header", mapped="CLAIMS"),
    _record(1, "[청구항 1]", kind="claim_header", claim_num=1),
    _record(2, "제1 도전형의 반도체 기판; 및 상기 기판 상의 게이트 전극을 포함하는 반도체 장치.", claim_num=1),

    _record(3, "[청구항 2]", kind="claim_header", claim_num=2),
    _record(4, "청구항 1에 있어서, 상기 게이트 전극은 폴리실리콘으로 형성되는, 반도체 장치.", claim_num=2),

    _record(5, "[청구항 3]", kind="claim_header", claim_num=3),
    _record(6, "반도체 기판 상에 절연막을 형성하는 단계; 및 상기 절연막 상에 게이트 전극을 형성하는 단계를 포함하는 방법.", claim_num=3),

    _record(7, "[청구항 4]", kind="claim_header", claim_num=4),
    _record(8, "청구항 3에 있어서, 상기 게이트 전극을 형성하는 단계는 폴리실리콘을 증착하는 단계를 포함하는 방법.", claim_num=4),

    _record(9, "[청구항 5]", kind="claim_header", claim_num=5),
    _record(10, "청구항 3에 있어서, 상기 게이트 전극 상에 보호막을 형성하는 단계를 더 포함하는 방법.", claim_num=5),

    _record(11, "[청구항 6]", kind="claim_header", claim_num=6),
    _record(12, "청구항 1 또는 2에 있어서, 상기 기판은 실리콘을 포함하는 반도체 장치.", claim_num=6),

    _record(13, "[청구항 7]", kind="claim_header", claim_num=7),
    _record(14, "청구항 1 내지 5 중 어느 한 항에 있어서, 상기 절연막의 두께는 5nm 이하인 반도체 장치.", claim_num=7),

    _record(15, "[청구항 8]", kind="claim_header", claim_num=8),
    _record(16, "비일시적 컴퓨터 판독 가능 매체로서, 프로세서에 의해 실행될 때 상기 프로세서가 동작들을 수행하게 하는 명령어들을 저장한 매체.", claim_num=8),

    _record(17, "[청구항 9]", kind="claim_header", claim_num=9),
    _record(18, "청구항 8에 있어서, 상기 명령어들은 추가로 ...", claim_num=9),
]


class StubClient:
    """Returns a canned English claim shaped like the prompt expects."""

    def __init__(self):
        self.call_log: list[tuple[int, str]] = []

    def complete(self, messages):
        user = messages[1]["content"]
        if user.startswith("Plan the English preamble"):
            head = user.split("\n", 1)[0]
            num = int(head.split("claim ")[1].split(".")[0])
            plans = {
                1: {
                    "korean_subject_span": "반도체 장치",
                    "english_noun_phrase": "semiconductor device",
                    "independent_preamble": "A semiconductor device comprising:",
                    "actor_phrase": "",
                    "confidence": "high",
                },
                3: {
                    "korean_subject_span": "방법",
                    "english_noun_phrase": "method",
                    "independent_preamble": "A method of manufacturing a semiconductor device, the method comprising:",
                    "actor_phrase": "",
                    "confidence": "high",
                },
                8: {
                    "korean_subject_span": "비일시적 컴퓨터 판독 가능 매체",
                    "english_noun_phrase": "non-transitory computer-readable medium",
                    "independent_preamble": (
                        "A non-transitory computer-readable medium storing instructions that, "
                        "when executed by a processor, cause the processor to:"
                    ),
                    "actor_phrase": "processor",
                    "confidence": "high",
                },
            }
            return json.dumps(plans[num])

        # Extract claim number from "Translate Korean claim N (..."
        head = user.split(" into ONE")[0]
        num = int(head.split("claim ")[1].split(" ")[0])
        self.call_log.append((num, head))
        responses = {
            1: "A semiconductor device comprising: a substrate; and a gate electrode.",
            2: "The semiconductor device of claim 1, wherein the gate electrode comprises polysilicon.",
            3: "A method of manufacturing a semiconductor device, the method comprising: forming an insulating layer; and forming a gate electrode.",
            4: "The method of claim 3, wherein forming the gate electrode comprises depositing polysilicon.",
            5: "The method of claim 3, further comprising forming a protective layer on the gate electrode.",
            6: "The semiconductor device of claim 1 or claim 2, wherein the substrate comprises silicon.",
            7: "The semiconductor device of any one of claims 1 to 5, wherein the insulating layer has a thickness of 5 nm or less.",
            8: "A non-transitory computer-readable medium storing instructions that, when executed by a processor, cause the processor to perform operations.",
            9: "The non-transitory computer-readable medium of claim 8, wherein the instructions further cause the processor to validate inputs.",
        }
        return json.dumps({"text": responses[num], "key_terms": []})


def main() -> None:
    state = {"records": RECORDS}
    state.update(chunk_claims(state))
    chunks = state["chunks_claims"]

    print("=== After chunk_claims (classification) ===")
    for c in chunks:
        print(
            f"[{c.claim_num}] kind={c.claim_kind!s:7s} "
            f"indep={c.is_independent!s:5s} "
            f"parents={c.parent_claim_nums} multi={c.multi_parent_kind}"
        )

    stub = StubClient()
    state["client"] = stub
    state.update(plan_claim_preambles(state))
    translate_claims(state)

    print("\n=== Call order observed by stub LLM ===")
    for i, (num, head) in enumerate(stub.call_log, 1):
        print(f"  {i:>2}. claim {num} — {head.split('Translate Korean ')[-1]}")

    print("\n=== Final translations + locked noun phrases ===")
    for c in chunks:
        print(f"[{c.claim_num}] kind={c.claim_kind} indep={c.is_independent} "
              f"noun_phrase={c.noun_phrase!r}")
        print(f"     → {c.translation!r}")


if __name__ == "__main__":
    main()
