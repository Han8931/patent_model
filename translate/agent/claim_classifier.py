"""Korean patent claim classifier — deterministic structural pre-pass.

For every claim chunk we want, BEFORE calling the LLM:
  - claim_kind        : device | method | crm | system
  - is_independent    : True / False
  - parent_claim_nums : list[int] (empty for independents)
  - multi_parent_kind : "single" | "or" | "range"   (drives English form)

This gives us:
  - Reliable preamble templates per kind.
  - Two-stage translation: independents first (lock noun phrase), dependents
    second (use parent's locked preamble).
  - Cross-kind dependency detection (warn if a dependent disagrees with its parent).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

ClaimKind = Literal["device", "method", "crm", "system"]
MultiParent = Literal["single", "or", "range"]


# ---------------------------------------------------------------------------
# Dependency phrase detection
# ---------------------------------------------------------------------------
# Korean dependent phrases come in modern and legacy forms:
#   modern : 청구항 N에 있어서 / 청구항 N에 따른 / 청구항 N의 ...
#   legacy : 제 N 항에 있어서 / 제 N 항에 따른
#
# Multi-dependent variants:
#   or     : 청구항 1 또는 2에 있어서        → "claim 1 or claim 2"
#   range  : 청구항 1 내지 5 중 어느 한 항    → "any one of claims 1 to 5"

_DEP_RANGE = re.compile(
    r'(?:청구항\s*|제\s*)?(\d+)\s*(?:항\s*)?내지\s*'
    r'(?:청구항\s*|제\s*)?(\d+)\s*(?:항)?'
)
_DEP_OR = re.compile(
    r'(?:청구항\s*|제\s*)?(\d+)\s*(?:항)?\s*(?:또는|혹은)\s*'
    r'(?:청구항\s*|제\s*)?(\d+)\s*(?:항)?'
)
_DEP_SINGLE = re.compile(
    r'(?:청구항\s*|제\s*)(\d+)\s*(?:항)?\s*(?:에\s*있어서|에\s*따른|의)'
)


def parse_dependency(text: str) -> tuple[list[int], MultiParent]:
    """Return (parent_claim_nums, multi_kind). Empty list ⇒ independent."""
    if "내지" in text:
        m = _DEP_RANGE.search(text)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b:
                return list(range(a, b + 1)), "range"
    if "또는" in text or "혹은" in text:
        m = _DEP_OR.search(text)
        if m:
            return [int(m.group(1)), int(m.group(2))], "or"
    m = _DEP_SINGLE.search(text)
    if m:
        return [int(m.group(1))], "single"
    return [], "single"


# ---------------------------------------------------------------------------
# Kind detection
# ---------------------------------------------------------------------------
# Patterns ordered most-specific first. CRM beats method beats system beats device.
# We match against the trailing "...을 포함하는 X" if present (most reliable signal),
# otherwise scan the whole text.

_TRAILING_NOUN_RE = re.compile(
    r'(?:을|를)\s*포함하는\s*([^\.。\n]+?)\s*[\.。]?\s*$'
)

_CRM_PAT = re.compile(
    r'(?:비\s*일시적\s*)?(?:컴퓨터(?:로)?\s*판독\s*가능(?:한|하게)?\s*)?'
    r'(?:기록|저장)\s*매체|'
    r'(?:컴퓨터\s*)?프로그램\s*제품|'
    r'(?:비\s*일시적\s*)?컴퓨터\s*판독\s*가능(?:한)?\s*매체'
)
_METHOD_PAT = re.compile(
    r'(?<![가-힣])(?:방법|프로세스)(?![가-힣])|하는\s*단계|단계[를을]?\s*포함'
)
_SYSTEM_PAT = re.compile(r'(?<![가-힣])시스템(?![가-힣])')
_DEVICE_PAT = re.compile(
    r'장치|디바이스|반도체|회로|모듈|기판|어셈블리|소자|패키지'
)


def _detect_kind_from(text: str) -> ClaimKind | None:
    if _CRM_PAT.search(text):
        return "crm"
    if _METHOD_PAT.search(text):
        return "method"
    if _SYSTEM_PAT.search(text):
        return "system"
    if _DEVICE_PAT.search(text):
        return "device"
    return None


def detect_kind(text: str, *, default: ClaimKind = "device") -> ClaimKind:
    """Best-effort kind detection from claim text.

    Prefer the trailing "...을 포함하는 X" subject (most reliable patent-style
    signal). If absent, scan the full text. Default to ``default`` when no
    pattern matches — for dependents this is typically the parent's kind.
    """
    m = _TRAILING_NOUN_RE.search(text)
    if m:
        kind = _detect_kind_from(m.group(1))
        if kind is not None:
            return kind
    kind = _detect_kind_from(text)
    return kind if kind is not None else default


# ---------------------------------------------------------------------------
# Method-dependent connective (wherein vs further comprising)
# ---------------------------------------------------------------------------

_FURTHER_COMPRISING_RE = re.compile(
    r'(?:단계[를을]?\s*)?더\s*포함|를\s*더\s*포함하는|더\s*포함하는\s*단계'
)


def method_dependent_connective(korean_text: str) -> str:
    """Pick 'further comprising' vs 'wherein' for a method dependent claim."""
    if _FURTHER_COMPRISING_RE.search(korean_text):
        return "further comprising"
    return "wherein"


# ---------------------------------------------------------------------------
# Public spec
# ---------------------------------------------------------------------------

@dataclass
class ClaimSpec:
    claim_num: int
    claim_kind: ClaimKind
    is_independent: bool
    parent_claim_nums: list[int] = field(default_factory=list)
    multi_parent_kind: MultiParent = "single"


@dataclass
class PreambleSpec:
    """Locked preamble info from a translated INDEPENDENT claim.

    Populated after the LLM returns. Consumed when translating that claim's
    dependents — they reuse ``noun_phrase`` (and ``actor_phrase`` for CRM/system)
    verbatim so the dependent preamble is always 'The <noun_phrase> of claim N'.
    """
    claim_num: int
    claim_kind: ClaimKind
    noun_phrase: str
    actor_phrase: str | None = None  # CRM / system: "the processor", "the system"


# ---------------------------------------------------------------------------
# Noun-phrase extraction from translated independent claims.
# ---------------------------------------------------------------------------

# Device: 'A semiconductor device comprising:' → 'semiconductor device'
_DEVICE_NOUN_RE = re.compile(
    r'^A[n]?\s+(.+?)\s+comprising\b', re.IGNORECASE | re.DOTALL
)
# CRM: 'A non-transitory computer-readable medium storing instructions that, when
#       executed by a processor, cause the processor to:'
_CRM_NOUN_RE = re.compile(
    r'^A\s+((?:non-transitory\s+)?(?:computer-readable\s+)?medium)\b',
    re.IGNORECASE,
)
_CRM_ACTOR_RE = re.compile(
    r'cause\s+(?:the\s+)?(.+?)\s+to\b', re.IGNORECASE
)
# System: 'A system comprising:' / 'A system comprising: a processor; and a memory ...'
_SYSTEM_NOUN_RE = re.compile(r'^A\s+(system)\b', re.IGNORECASE)
_SYSTEM_ACTOR_RE = re.compile(
    r'cause\s+(?:the\s+)?(.+?)\s+to\b', re.IGNORECASE
)


def extract_preamble(translation: str, kind: ClaimKind) -> tuple[str, str | None]:
    """Return (noun_phrase, actor_phrase) extracted from the LLM's English output.

    Falls back to safe defaults so dependent claims still produce a valid
    preamble even if the independent translation drifted.
    """
    text = translation.strip()
    if kind == "method":
        # Dependents always say 'The method of claim N' regardless of the
        # specific gerund object in the independent preamble.
        return "method", None
    if kind == "crm":
        m = _CRM_NOUN_RE.search(text)
        noun = m.group(1).lower() if m else "non-transitory computer-readable medium"
        a = _CRM_ACTOR_RE.search(text)
        actor = a.group(1).strip().lower() if a else "processor"
        return noun, actor
    if kind == "system":
        m = _SYSTEM_NOUN_RE.search(text)
        noun = m.group(1).lower() if m else "system"
        a = _SYSTEM_ACTOR_RE.search(text)
        actor = a.group(1).strip().lower() if a else None
        return noun, actor
    # device
    m = _DEVICE_NOUN_RE.search(text)
    noun = m.group(1).strip() if m else "apparatus"
    return noun, None


# ---------------------------------------------------------------------------
# Dependent preamble prefix construction.
# ---------------------------------------------------------------------------

def format_parent_reference(
    parent_claim_nums: list[int],
    multi_parent_kind: MultiParent,
) -> str:
    """English form of the parent claim reference."""
    if not parent_claim_nums:
        return ""
    if multi_parent_kind == "range" and len(parent_claim_nums) >= 2:
        return f"any one of claims {parent_claim_nums[0]} to {parent_claim_nums[-1]}"
    if multi_parent_kind == "or" and len(parent_claim_nums) >= 2:
        # Up to 2 cited parents in 'A 또는 B' form.
        return f"claim {parent_claim_nums[0]} or claim {parent_claim_nums[1]}"
    return f"claim {parent_claim_nums[0]}"


def build_dependent_preamble(
    parent_spec: PreambleSpec,
    parent_claim_nums: list[int],
    multi_parent_kind: MultiParent,
) -> str:
    """Build the literal dependent preamble prefix (without the connective).

    Examples:
      'The semiconductor device of claim 1'
      'The method of claim 3'
      'The non-transitory computer-readable medium of any one of claims 1 to 5'
    """
    ref = format_parent_reference(parent_claim_nums, multi_parent_kind)
    return f"The {parent_spec.noun_phrase} of {ref}"


def classify_claim(
    claim_num: int,
    text: str,
    *,
    parent_kind: ClaimKind | None = None,
) -> ClaimSpec:
    """Classify a single claim. Pass ``parent_kind`` for dependents to inherit."""
    parents, multi_kind = parse_dependency(text)
    is_indep = not parents
    if is_indep:
        kind: ClaimKind = detect_kind(text)
    else:
        kind = detect_kind(text, default=parent_kind or "device")
        if parent_kind is not None:
            # Dependents almost always share the parent's kind. Trust the
            # parent unless the dependent's own text loudly contradicts
            # (e.g. the parent is "device" but the dependent text contains
            # explicit method markers like "하는 단계").
            own = _detect_kind_from(text)
            if own is None or own == parent_kind:
                kind = parent_kind
            else:
                kind = own  # surface the disagreement; caller may warn
    return ClaimSpec(
        claim_num=claim_num,
        claim_kind=kind,
        is_independent=is_indep,
        parent_claim_nums=parents,
        multi_parent_kind=multi_kind,
    )
