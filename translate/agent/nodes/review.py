"""Review nodes — decide-then-revise per section.

Each section gets:
  review_decide_<section>:  cheap call → {needs_revision, issues}
  review_revise_<section>:  expensive call → list of revisions

A conditional edge between them skips the revise step when needs_revision == False.
"""

from __future__ import annotations

from typing import Callable, Literal

from ..docx_utils import _normalize_unicode, postprocess
from ..glossary import clean_translation_text, extract_json_block
from ..nodes.translate_claims import (
    _assert_claims_translated,
    _contains_hangul,
    _minimal_cleanup,
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


def _truncate_for_log(text: str, limit: int = 400) -> str:
    """Compact one-line preview suitable for the progress / .log stream."""
    flat = (text or "").replace("\n", " ⏎ ").replace("\t", " ⇥ ")
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


def _log_revision(
    progress: Callable[[str], None],
    kind: str,
    target: Chunk,
    pair_index: int,
    issues: list[str],
    before: str,
    after: str,
) -> None:
    """Print a structured BEFORE/AFTER record for each applied revision.

    Format:

        [REVISION claims chunk-7 (claim 7)]
          ISSUE 1: Claim 7 'first semiconductor die comprises a second substrate' is confusing
          ISSUE 2: ...
          BEFORE: A semiconductor package, comprising: a first die ⏎ ...
          AFTER : A semiconductor package, comprising: a first die ⏎ ...

    Every line goes through ``progress`` so the same record lands on stdout
    AND in the per-document ``.log`` file. Long translations are truncated
    to a configurable preview length to keep individual revisions on a few
    log lines instead of hundreds.
    """
    label_bits: list[str] = [f"{kind}", f"chunk-{target.id}"]
    if target.claim_num is not None:
        label_bits.append(f"(claim {target.claim_num})")
    header = f"[REVISION {' '.join(label_bits)}]"
    progress(header)
    for i, issue in enumerate(issues or [], 1):
        progress(f"  ISSUE {i}: {issue}")
    progress(f"  BEFORE: {_truncate_for_log(before)}")
    progress(f"  AFTER : {_truncate_for_log(after)}")


def _pairs_from(chunks: list[Chunk]) -> list[tuple[str, str]]:
    return [
        (c.text, c.translation or "")
        for c in chunks
        if c.translation
    ]




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
        issues_list: list[str] = decision.get("issues") or []
        applied = 0
        for item in revisions:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            text = item.get("text")
            if isinstance(idx, int) and isinstance(text, str) and 0 <= idx < len(translated_chunks):
                target = translated_chunks[idx]
                before = target.translation or ""
                if kind == "claims":
                    # Minimal cleanup matches the bulk translate_claims path.
                    if _contains_hangul(text):
                        if verbose:
                            claim_num = target.claim_num
                            print(
                                f"  [REVIEW {kind}] Skipped claim {claim_num} "
                                "revision containing Korean/Hangul text."
                            )
                        continue
                    claim_num = target.claim_num
                    text = _minimal_cleanup(claim_num, text) if claim_num is not None else _normalize_unicode(text.strip())
                    if not text:
                        continue
                else:
                    text = clean_translation_text(text)
                    if not text or _contains_hangul(text):
                        if verbose:
                            print(
                                f"  [REVIEW {kind}] Skipped revision with "
                                "empty, meta, or Korean/Hangul text."
                            )
                        continue
                    text = postprocess(text)
                if text == before:
                    # No-op revision — don't pollute the log with empty diffs.
                    continue
                target.translation = text
                applied += 1
                _log_revision(
                    progress, kind, target, idx, issues_list, before, text,
                )

        if kind == "claims":
            _assert_claims_translated(chunks)

        progress(f"  [REVIEW {kind}] Applied {applied} revision(s).")

        return {_state_key_for(kind): chunks}

    return revise


def needs_revision(kind: SectionKind) -> Callable[[TranslationState], str]:
    """Conditional edge: returns 'revise' or 'skip' based on the decision result."""
    def cond(state: TranslationState) -> str:
        decision = state.get(f"_review_{kind}_decision") or {}
        return "revise" if decision.get("needs_revision") else "skip"
    return cond
