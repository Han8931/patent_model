"""Review nodes — decide-then-revise per section.

Each section gets:
  review_decide_<section>:  cheap call → {needs_revision, issues}
  review_revise_<section>:  expensive call → list of revisions

A conditional edge between them skips the revise step when needs_revision == False.
"""

from __future__ import annotations

from typing import Callable, Literal

from ..claim_classifier import PreambleSpec, method_dependent_connective
from ..docx_utils import postprocess
from ..glossary import extract_json_block
from ..nodes.translate_claims import (
    _dependent_opening,
    _enforce_dependent_preamble,
)
from ..prompts import build_decision_messages, build_revision_messages
from ..state import Chunk, TranslationState


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
        if c.translation
    ]


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


def make_decide(kind: SectionKind) -> Callable[[TranslationState], dict]:
    def decide(state: TranslationState) -> dict:
        if not state.get("review", True):
            return {f"_review_{kind}_decision": {"needs_revision": False, "issues": []}}

        chunks = _chunks_for(state, kind)
        pairs = _pairs_from(chunks)
        if len(pairs) < 2:
            return {f"_review_{kind}_decision": {"needs_revision": False, "issues": []}}

        client = state["client"]
        glossary = state.get("glossary", {})
        progress = state.get("progress") or (lambda _: None)
        verbose = state.get("verbose", False)

        progress(f"Reviewing {_section_label(kind)}…")
        try:
            raw = client.complete(build_decision_messages(_section_label(kind), pairs, glossary))
            decision = extract_json_block(raw) or {}
            if not isinstance(decision, dict):
                decision = {"needs_revision": False, "issues": []}
        except Exception as exc:
            if verbose:
                print(f"  review_decide_{kind} failed: {exc}")
            decision = {"needs_revision": False, "issues": []}

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
        if len(pairs) < 2:
            return {}

        client = state["client"]
        glossary = state.get("glossary", {})
        progress = state.get("progress") or (lambda _: None)
        verbose = state.get("verbose", False)

        progress(f"Revising {_section_label(kind)}…")
        try:
            raw = client.complete(build_revision_messages(
                _section_label(kind), pairs,
                decision.get("issues") or [],
                glossary,
            ))
            revisions = extract_json_block(raw)
            if not isinstance(revisions, list):
                revisions = []
        except Exception as exc:
            if verbose:
                print(f"  review_revise_{kind} failed: {exc}")
            return {}

        # The pairs index corresponds to chunks-with-a-translation; map back to chunks.
        translated_chunks = [c for c in chunks if c.translation]
        claim_specs = _claim_preamble_specs(chunks) if kind == "claims" else {}
        applied = 0
        for item in revisions:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            text = item.get("text")
            if isinstance(idx, int) and isinstance(text, str) and 0 <= idx < len(translated_chunks):
                if kind == "claims":
                    text = _enforce_revised_claim_preamble(
                        translated_chunks[idx],
                        text,
                        claim_specs,
                    )
                translated_chunks[idx].translation = postprocess(text)
                applied += 1

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
