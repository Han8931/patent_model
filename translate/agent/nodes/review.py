"""Review nodes — decide-then-revise per section.

Each section gets:
  review_decide_<section>:  cheap call → {needs_revision, issues}
  review_revise_<section>:  expensive call → list of revisions

A conditional edge between them skips the revise step when needs_revision == False.
"""

from __future__ import annotations

import re
from typing import Callable, Literal

from ..claim_classifier import PreambleSpec, method_dependent_connective
from ..docx_utils import postprocess
from ..glossary import extract_json_block
from ..nodes.translate_claims import (
    _assert_claims_translated,
    _claim_style_problem,
    _contains_hangul,
    _dependent_opening,
    _enforce_dependent_preamble,
    _format_translation,
)
from ..prompts import (
    build_decision_messages,
    build_revision_messages,
    build_single_claim_revision_messages,
)
from ..state import Chunk, TranslationState
from ..validation import source_reference_tokens, translation_problem


SectionKind = Literal["body", "abstract", "claims"]


def _chunks_for(state: TranslationState, kind: SectionKind) -> list[Chunk]:
    if kind == "body":
        return state.get("chunks_body", [])
    if kind == "abstract":
        return state.get("chunks_abstract", [])
    return state.get("chunks_claims", [])


def _section_label(kind: SectionKind) -> str:
    return {"body": "BODY", "abstract": "ABSTRACT", "claims": "CLAIMS"}[kind]


def _state_key_for(kind: SectionKind) -> str:
    return f"chunks_{kind}"


def _pairs_from(chunks: list[Chunk]) -> list[tuple[str, str]]:
    return [
        (c.text, c.translation or "")
        for c in chunks
        if not getattr(c, "applied_in_place", False)
    ]


_SOURCE_REF_RE = re.compile(
    r'(?<![A-Za-z0-9_])[\(\[](?P<ref>(?:[A-Z]{2,8}|(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{1,12}))[\)\]]'
)
# Any bracketed reference character. This includes pure-letter patent reference
# characters such as (LD) and (GC), which Korean specs often use like numerals.
# ``[EQUATION_N]`` and FIG. patterns are skipped below.
_BAD_EN_REF_BRACKET_RE = re.compile(
    r'[\(\[](?P<ref>(?:[A-Z]{2,8}|(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{1,12}))[\)\]]'
)
_PARAGRAPH_ID_RE = re.compile(r'(?m)^\s*\[\d{1,5}\]')
_EQUATION_RE = re.compile(r'\[EQUATION(?:_\d+)?\]')
_KOREAN_BRACKET_CONTENT_RE = re.compile(r'[\(\[（［](?P<inner>[^()\[\]（）［］]{1,120}[가-힣][^()\[\]（）［］]{0,120})[\)\]）］]')
_KOREAN_TOKEN_RE = re.compile(r'[가-힣A-Za-z0-9]+')
_GENERIC_KOREAN_BRACKET_TOKENS = {
    "상기", "전술", "이하", "이상", "예", "예를", "예컨대", "선택적",
    "또는", "및", "중", "적어도", "하나", "복수", "포함", "포함하는",
}
_BRACKET_GLOSSARY_FALLBACK: dict[str, tuple[str, ...]] = {
    "전기": ("electric", "electrical"),
    "광": ("light", "optical", "optically"),
    "제어": ("control", "controller", "controlling"),
    "신호": ("signal",),
    "전압": ("voltage",),
    "전류": ("current",),
    "전극": ("electrode",),
    "도파로": ("waveguide",),
    "증폭기": ("amplifier",),
    "서브": ("sub",),
    "기판": ("substrate",),
    "층": ("layer",),
    "막": ("film", "layer"),
    "영역": ("region", "area"),
    "홈": ("groove", "recess"),
    "깊이": ("depth",),
    "폭": ("width",),
    "길이": ("length",),
}


def _source_reference_chars(text: str) -> set[str]:
    refs = source_reference_tokens(text)
    # Do not report paragraph IDs as ordinary missing reference numerals.
    return {ref for ref in refs if not (ref.isdigit() and len(ref) <= 5)}


def _bad_english_reference_brackets(text: str) -> list[str]:
    cleaned = _PARAGRAPH_ID_RE.sub("", text)
    bad: list[str] = []
    for m in _BAD_EN_REF_BRACKET_RE.finditer(cleaned):
        ref = m.group("ref")
        if ref.upper().startswith("EQUATION"):
            continue
        prefix_window = cleaned[max(0, m.start() - 8):m.start()]
        if re.search(r'\bFIGS?\.?\s*$', prefix_window.upper()):
            continue
        # For compact forms ``GR(1)``, attach the immediately preceding
        # uppercase/digit prefix so the report names the full pattern instead
        # of just the inner digits.
        attached = re.search(r'[A-Za-z][A-Za-z0-9_-]*$', prefix_window)
        token = m.group(0)
        if attached:
            token = attached.group(0) + token
        bad.append(token)
    return bad


def _english_has_ref(text: str, ref: str) -> bool:
    return re.search(rf'(?<![A-Za-z0-9_-]){re.escape(ref)}(?![A-Za-z0-9_-])', text) is not None


def _source_bracketed_claim_tokens(text: str) -> list[tuple[str, list[str]]]:
    items: list[tuple[str, list[str]]] = []
    for m in _KOREAN_BRACKET_CONTENT_RE.finditer(text):
        inner = m.group("inner").strip()
        tokens: list[str] = []
        for token in _KOREAN_TOKEN_RE.findall(inner):
            if token in _GENERIC_KOREAN_BRACKET_TOKENS:
                continue
            if token.isdigit():
                tokens.append(token)
                continue
            if re.fullmatch(r'[A-Za-z0-9]+', token):
                tokens.append(token)
                continue
            if len(token) >= 2:
                tokens.append(token)
        if tokens:
            items.append((inner, tokens))
    return items


def _english_covers_bracket_token(token: str, english: str, glossary: dict) -> bool:
    lowered = english.lower()
    if re.fullmatch(r'[A-Za-z0-9]+', token):
        return re.search(rf'(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])', english, re.IGNORECASE) is not None
    mapped = glossary.get(token)
    candidates: list[str] = []
    if isinstance(mapped, str) and mapped.strip():
        candidates.append(mapped.strip().lower())
    candidates.extend(_BRACKET_GLOSSARY_FALLBACK.get(token, ()))
    return any(candidate and candidate in lowered for candidate in candidates)


def _claim_source_item_count(text: str) -> int:
    return (
        text.count(";")
        + len(re.findall(r'\s및\s|\s또는\s', text))
        + len(re.findall(r'단계', text))
    )


def _claim_english_item_count(text: str) -> int:
    return (
        text.count(";")
        + len(re.findall(r'\b(?:and|or)\b', text, flags=re.IGNORECASE))
        + len(re.findall(r'\n\t', text))
    )


def _deterministic_issues(
    kind: SectionKind,
    chunks: list[Chunk],
    glossary: dict | None = None,
) -> list[str]:
    glossary = glossary or {}
    issues: list[str] = []
    for idx, chunk in enumerate(chunks):
        if getattr(chunk, "applied_in_place", False):
            continue
        korean = chunk.text or ""
        english = chunk.translation or ""
        label = f"claim {chunk.claim_num}" if kind == "claims" and chunk.claim_num else f"paragraph {idx}"

        if not english.strip():
            issues.append(f"{label}: English translation is empty or missing.")
            continue

        bad_refs = _bad_english_reference_brackets(english)
        if bad_refs:
            issues.append(
                f"{label}: reference character(s) still appear in brackets "
                f"({', '.join(sorted(set(bad_refs)))}) instead of USPTO style without brackets."
            )

        missing_refs = sorted(
            ref for ref in _source_reference_chars(korean)
            if not _english_has_ref(english, ref)
        )
        if missing_refs:
            issues.append(
                f"{label}: missing source reference character(s) "
                f"{', '.join(missing_refs)} in the English translation."
            )

        if _EQUATION_RE.findall(korean) != _EQUATION_RE.findall(english):
            issues.append(f"{label}: [EQUATION] marker count/order differs from the Korean source.")

        if kind == "claims":
            if _contains_hangul(english):
                issues.append(f"{label}: claim translation still contains Korean/Hangul text.")
            missing_bracket_terms: list[str] = []
            for inner, tokens in _source_bracketed_claim_tokens(korean):
                missing = [
                    token for token in tokens
                    if not _english_covers_bracket_token(token, english, glossary)
                ]
                if missing:
                    missing_bracket_terms.append(f"{inner} -> {', '.join(missing)}")
            if missing_bracket_terms:
                issues.append(
                    f"{label}: source bracketed/parenthetical claim content appears omitted "
                    f"({'; '.join(missing_bracket_terms[:5])})."
                )
            ko_items = _claim_source_item_count(korean)
            en_items = _claim_english_item_count(english)
            if ko_items >= 2 and en_items + 1 < ko_items:
                issues.append(
                    f"{label}: English claim appears to omit limitations; "
                    f"source has about {ko_items} listed items/steps but English has about {en_items}."
                )
    return issues


def _claim_preamble_specs(chunks: list[Chunk]) -> dict[int, PreambleSpec]:
    specs: dict[int, PreambleSpec] = {}
    for c in chunks:
        if c.claim_num is None or not c.noun_phrase:
            continue
        specs[c.claim_num] = PreambleSpec(
            claim_num=c.claim_num,
            claim_kind=c.claim_kind or "device",
            noun_phrase=c.noun_phrase,
            actor_phrase=c.actor_phrase,
        )
    return specs


def _enforce_revised_claim_preamble(
    chunk: Chunk,
    text: str,
    specs: dict[int, PreambleSpec],
) -> str:
    if chunk.is_independent or not chunk.parent_claim_nums:
        return text
    parent_spec = None
    for parent_num in chunk.parent_claim_nums:
        if parent_num in specs:
            parent_spec = specs[parent_num]
            break
    if parent_spec is None:
        return text
    method_connective = "wherein"
    if chunk.claim_kind == "method":
        method_connective = method_dependent_connective(chunk.text)
    required = _dependent_opening(chunk, parent_spec, method_connective)
    return _enforce_dependent_preamble(text, required)


def _required_claim_opening(
    chunk: Chunk,
    specs: dict[int, PreambleSpec],
) -> str | None:
    if chunk.is_independent:
        return chunk.independent_preamble
    if not chunk.parent_claim_nums:
        return None
    parent_spec = None
    for parent_num in chunk.parent_claim_nums:
        if parent_num in specs:
            parent_spec = specs[parent_num]
            break
    if parent_spec is None:
        return None
    method_connective = "wherein"
    if chunk.claim_kind == "method":
        method_connective = method_dependent_connective(chunk.text)
    return _dependent_opening(chunk, parent_spec, method_connective)


def _validate_claim_revision(
    chunk: Chunk,
    text: str,
    specs: dict[int, PreambleSpec],
) -> tuple[str, str]:
    if chunk.claim_num is not None:
        text = re.sub(rf'^\s*{chunk.claim_num}\.\s*', '', text.strip(), count=1)
    text = _enforce_revised_claim_preamble(chunk, text, specs)
    text = postprocess(text)
    problem = translation_problem(text)
    if problem:
        return text, problem
    required = _required_claim_opening(chunk, specs)
    style_problem = _claim_style_problem(chunk, text, required)
    if style_problem:
        return text, style_problem
    if chunk.claim_num is not None:
        text = _format_translation(chunk.claim_num, text)
        problem = translation_problem(text)
        if problem:
            return text, problem
    return text, ""


def _claim_nums_from_issues(issues: list[str]) -> set[int]:
    nums: set[int] = set()
    for issue in issues:
        for m in re.finditer(r'\bclaim\s+(\d+)\b', issue, flags=re.IGNORECASE):
            nums.add(int(m.group(1)))
    return nums


def _issue_list_for_claim(claim_num: int | None, issues: list[str]) -> list[str]:
    if claim_num is None:
        return issues
    needle = re.compile(rf'\bclaim\s+{claim_num}\b', re.IGNORECASE)
    selected = [issue for issue in issues if needle.search(issue)]
    return selected or issues


def _fallback_revise_claims(
    *,
    chunks: list[Chunk],
    client,
    glossary: dict,
    issues: list[str],
    specs: dict[int, PreambleSpec],
    verbose: bool,
) -> int:
    """Try per-claim repair for claims still flagged after the bulk review."""
    deterministic = _deterministic_issues("claims", chunks, glossary)
    flagged_nums = _claim_nums_from_issues(issues + deterministic)
    if not flagged_nums:
        return 0

    by_num = {c.claim_num: c for c in chunks if c.claim_num is not None}
    applied = 0
    for claim_num in sorted(flagged_nums):
        chunk = by_num.get(claim_num)
        if chunk is None:
            continue
        required = _required_claim_opening(chunk, specs)
        messages = build_single_claim_revision_messages(
            korean_claim=chunk.text,
            english_claim=chunk.translation or "",
            issues=_issue_list_for_claim(claim_num, issues + deterministic),
            glossary=glossary,
            required_opening=required,
        )
        last_problem = ""
        for attempt in range(4):
            try:
                raw = client.complete(messages)
                text, problem = _validate_claim_revision(
                    chunk,
                    raw,
                    specs,
                )
                if not problem:
                    chunk.translation = text
                    applied += 1
                    break
                last_problem = problem
            except Exception as exc:
                last_problem = f"The per-claim revision call failed: {type(exc).__name__}: {exc}"
            if verbose:
                suffix = " Retrying..." if attempt < 3 else ""
                print(
                    f"  [REVIEW claims] fallback claim {claim_num}: "
                    f"{last_problem}{suffix}"
                )
            if attempt < 3:
                messages = messages + [{
                    "role": "user",
                    "content": (
                        "Try again. Output only one complete USPTO-style English claim. "
                        f"Problem to fix: {last_problem}"
                    ),
                }]
    return applied


def make_decide(kind: SectionKind) -> Callable[[TranslationState], dict]:
    def decide(state: TranslationState) -> dict:
        if not state.get("review", True):
            return {f"_review_{kind}_decision": {"needs_revision": False, "issues": []}}

        chunks = _chunks_for(state, kind)
        pairs = _pairs_from(chunks)
        if not pairs:
            return {f"_review_{kind}_decision": {"needs_revision": False, "issues": []}}

        client = state["client"]
        glossary = state.get("glossary", {})
        deterministic_issues = _deterministic_issues(kind, chunks, glossary)
        progress = state.get("progress") or (lambda _: None)
        verbose = state.get("verbose", False)

        progress(f"Reviewing {_section_label(kind)}…")
        messages = build_decision_messages(_section_label(kind), pairs, glossary)
        decision = {"needs_revision": False, "issues": []}
        last_problem = ""
        for attempt in range(4):
            try:
                raw = client.complete(messages)
                parsed = extract_json_block(raw)
                if isinstance(parsed, dict):
                    decision = parsed
                    break
                last_problem = "The review decision was not valid JSON object."
            except Exception as exc:
                last_problem = f"The review call failed: {type(exc).__name__}: {exc}"
            if verbose:
                suffix = " Retrying..." if attempt < 3 else ""
                print(f"  review_decide_{kind}: {last_problem}{suffix}")
            if attempt < 2:
                messages = messages + [{
                    "role": "user",
                    "content": (
                        "Return only valid JSON in this exact shape: "
                        '{"needs_revision": false, "issues": []} or '
                        '{"needs_revision": true, "issues": ["issue"]}.'
                    ),
                }]

        if deterministic_issues:
            existing = decision.get("issues") if isinstance(decision.get("issues"), list) else []
            decision["needs_revision"] = True
            decision["issues"] = existing + deterministic_issues

        if verbose:
            if decision.get("needs_revision"):
                for issue in decision.get("issues", []):
                    print(f"  [REVIEW {kind}] Issue: {issue}")
            else:
                print(f"  [REVIEW {kind}] No revision needed.")

        return {f"_review_{kind}_decision": decision}

    return decide


def make_revise(kind: SectionKind) -> Callable[[TranslationState], dict]:
    def revise(state: TranslationState) -> dict:
        decision = state.get(f"_review_{kind}_decision") or {}
        if not decision.get("needs_revision"):
            return {}

        chunks = _chunks_for(state, kind)
        pairs = _pairs_from(chunks)
        if not pairs:
            return {}

        client = state["client"]
        glossary = state.get("glossary", {})
        progress = state.get("progress") or (lambda _: None)
        verbose = state.get("verbose", False)
        issues = decision.get("issues") if isinstance(decision.get("issues"), list) else []

        progress(f"Revising {_section_label(kind)}…")
        messages = build_revision_messages(
                _section_label(kind), pairs,
                issues,
                glossary,
            )
        revisions = []
        last_problem = ""
        for attempt in range(4):
            try:
                raw = client.complete(messages)
                parsed = extract_json_block(raw)
                if isinstance(parsed, list):
                    revisions = parsed
                    break
                last_problem = "The revision response was not a valid JSON array."
            except Exception as exc:
                last_problem = f"The revision call failed: {type(exc).__name__}: {exc}"
            if verbose:
                suffix = " Retrying..." if attempt < 3 else ""
                print(f"  review_revise_{kind}: {last_problem}{suffix}")
            if attempt < 2:
                messages = messages + [{
                    "role": "user",
                    "content": (
                        "Return only a valid JSON array like "
                        '[{"index": 0, "text": "revised English text"}]. '
                        "Use [] if no paragraph needs revision."
                    ),
                }]

        # The pairs index corresponds to reviewable chunks.
        translated_chunks = [
            c for c in chunks if not getattr(c, "applied_in_place", False)
        ]
        claim_specs = _claim_preamble_specs(chunks) if kind == "claims" else {}
        applied = 0
        for item in revisions:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            text = item.get("text")
            if isinstance(idx, int) and isinstance(text, str) and 0 <= idx < len(translated_chunks):
                if kind == "claims":
                    text, problem = _validate_claim_revision(
                        translated_chunks[idx],
                        text,
                        claim_specs,
                    )
                    if problem:
                        if verbose:
                            claim_num = translated_chunks[idx].claim_num
                            print(
                                f"  [REVIEW {kind}] Skipped claim {claim_num} "
                                f"revision: {problem}"
                            )
                        continue
                else:
                    text = postprocess(text)
                    problem = translation_problem(text)
                    if problem:
                        if verbose:
                            print(
                                f"  [REVIEW {kind}] Skipped revision {idx}: {problem}"
                            )
                        continue
                translated_chunks[idx].translation = text
                applied += 1

        if kind == "claims":
            fallback_applied = _fallback_revise_claims(
                chunks=chunks,
                client=client,
                glossary=glossary,
                issues=issues,
                specs=claim_specs,
                verbose=verbose,
            )
            applied += fallback_applied
            remaining = _deterministic_issues("claims", chunks, glossary)
            if remaining:
                raise RuntimeError(
                    "Claim review detected unresolved issue(s) after revision: "
                    + "; ".join(remaining[:10])
                )
            if issues and applied == 0:
                raise RuntimeError(
                    "Claim review detected issue(s), but no claim revisions were applied. "
                    "Refusing to continue with potentially omitted or defective claims."
                )
            _assert_claims_translated(chunks)

        if verbose:
            print(f"  [REVIEW {kind}] Applied {applied} revision(s).")

        return {_state_key_for(kind): chunks}

    return revise


def needs_revision(kind: SectionKind) -> Callable[[TranslationState], str]:
    """Conditional edge: returns 'revise' or 'skip' based on the decision result."""
    def cond(state: TranslationState) -> str:
        decision = state.get(f"_review_{kind}_decision") or {}
        return "revise" if decision.get("needs_revision") else "skip"
    return cond
