"""translate_claims — two-stage translation driven by deterministic claim classification.

Phase 1 — INDEPENDENTS, in claim-number order
    Translate every independent claim using the per-kind prompt
    (device/method/crm/system). Extract the locked-in noun phrase (and actor
    phrase for CRM/system) from the LLM's English output and store it in a
    PreambleSpec keyed by claim number.

Phase 2 — DEPENDENTS, in source order
    For each dependent claim, look up the parent's PreambleSpec and inject
    the literal preamble prefix ('The <noun_phrase> of claim N, wherein …')
    into the user prompt. Method dependents pick 'wherein' vs
    'further comprising' from the Korean source before the LLM call.

Equation paragraphs are still preserved in their original positions by the
write node — translation here is purely textual.
"""

from __future__ import annotations

import re
import time

from ..claim_classifier import (
    PreambleSpec,
    build_dependent_preamble,
    extract_preamble,
    method_dependent_connective,
)
from ..docx_utils import postprocess
from ..glossary import clean_translation_text, extract_json_block, merge_terms
from ..prompts import build_claim_messages
from ..sections import CLAIM_ELEMENT_CAP_RE, LLM_CLAIM_PREFIX_RE
from ..state import Chunk, TranslationState


def _format_translation(claim_num: int, raw_text: str) -> str:
    """Strip LLM-added claim numbers, lowercase after ';'/':',  prepend 'N. '."""
    text = LLM_CLAIM_PREFIX_RE.sub('', raw_text.strip())
    text = CLAIM_ELEMENT_CAP_RE.sub(lambda m: '\n' + m.group(1).lower(), text)
    return f"{claim_num}. {text}"


def _enforce_independent_preamble(text: str, planned: str | None) -> str:
    """Conservatively replace the opening preamble with the planned one.

    The planner is responsible for semantic preamble selection. This function
    only enforces the already-planned opening when the translation has a normal
    claim preamble ending in ':' near the start.
    """
    if not planned:
        return text
    text = text.strip()
    planned = planned.strip()
    if not text or text.startswith(planned):
        return text
    match = re.match(r'^[A-Z][^:\n]{0,220}:', text)
    if not match:
        return text
    return planned + text[match.end():]


def _dependent_opening(
    chunk: Chunk,
    parent_spec: PreambleSpec | None,
    method_connective: str,
) -> str | None:
    if parent_spec is None or not chunk.parent_claim_nums:
        return None
    preamble = build_dependent_preamble(
        parent_spec,
        chunk.parent_claim_nums,
        chunk.multi_parent_kind,
    )
    if chunk.claim_kind == "method" and method_connective == "further comprising":
        return f"{preamble}, further comprising"
    return f"{preamble}, wherein"


def _enforce_dependent_preamble(text: str, required: str | None) -> str:
    """Ensure a dependent claim keeps its claim-reference opening.

    LLMs occasionally rewrite dependents as independent claims
    ("A semiconductor package comprising:"). When we have a parent-derived
    opening, replace any independent-style opening through the first ':' or
    comma with the required dependent opening.
    """
    if not required:
        return text
    text = text.strip()
    required = required.strip()
    if not text:
        return text
    if text.lower().startswith(required.lower()):
        return required + text[len(required):]

    independent = re.match(r'^A[n]?\s+[^:\n]{1,220}:\s*', text)
    if independent:
        if required.lower().endswith(", wherein"):
            noun_match = re.match(r'^The\s+(.+?)\s+of\s+', required)
            if noun_match:
                noun = noun_match.group(1)
                return (
                    required
                    + f" the {noun} comprises "
                    + text[independent.end():].lstrip()
                )
        return required + " " + text[independent.end():].lstrip()

    dependent = re.match(r'^The\s+[^,\n]{1,220},\s*(?:wherein|further\s+comprising)\b\s*', text, re.IGNORECASE)
    if dependent:
        return required + " " + text[dependent.end():].lstrip()

    return required + " " + text


def _translate_one(
    chunk: Chunk,
    *,
    client,
    glossary: dict[str, str],
    parent_spec: PreambleSpec | None,
    verbose: bool,
) -> dict | None:
    """Run one LLM call for ``chunk``. Mutates chunk.translation; returns key_terms."""
    method_connective = "wherein"
    if chunk.claim_kind == "method" and not chunk.is_independent:
        method_connective = method_dependent_connective(chunk.text)
    required_dependent_opening = _dependent_opening(
        chunk, parent_spec, method_connective
    )

    try:
        messages = build_claim_messages(
            claim_num=chunk.claim_num,
            chunk_text=chunk.text,
            glossary=glossary,
            kind=chunk.claim_kind or "device",
            is_independent=bool(chunk.is_independent),
            parent_spec=parent_spec,
            parent_claim_nums=chunk.parent_claim_nums,
            multi_parent_kind=chunk.multi_parent_kind,
            method_connective=method_connective,
            equation_context=chunk.equation_context,
            independent_preamble=chunk.independent_preamble,
        )
        raw = client.complete(messages)
        data = extract_json_block(raw) or {}
        text = clean_translation_text(data.get("text")) or clean_translation_text(raw)
        if text:
            if chunk.is_independent:
                text = _enforce_independent_preamble(
                    text, chunk.independent_preamble
                )
            else:
                text = _enforce_dependent_preamble(
                    text, required_dependent_opening
                )
            chunk.translation = postprocess(_format_translation(chunk.claim_num, text))
        else:
            if verbose:
                print(
                    f"  translate_claims claim {chunk.claim_num}: "
                    "empty/placeholder output, keeping Korean"
                )
            chunk.translation = f"{chunk.claim_num}. {chunk.text}"
        return data
    except Exception as exc:
        if verbose:
            print(f"  translate_claims claim {chunk.claim_num} failed: {exc}")
        chunk.translation = f"{chunk.claim_num}. {chunk.text}"
        return None


def translate_claims(state: TranslationState) -> dict:
    chunks: list[Chunk] = state.get("chunks_claims", [])
    if not chunks:
        return {}

    client = state["client"]
    glossary = dict(state.get("glossary", {}))
    delay = state.get("delay", 0.0)
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)

    valid = [c for c in chunks if c.claim_num is not None]
    total = len(valid)
    progress(f"Translating CLAIMS ({total} claims)…")

    preamble_specs: dict[int, PreambleSpec] = {}
    done = 0

    # ----- Phase 1: independents in claim-number order -----------------------
    independents = sorted(
        (c for c in valid if c.is_independent),
        key=lambda c: c.claim_num,
    )
    for chunk in independents:
        data = _translate_one(
            chunk, client=client, glossary=glossary,
            parent_spec=None, verbose=verbose,
        )
        if data is not None:
            merge_terms(glossary, data.get("key_terms") or [])

        if chunk.translation:
            # Strip the leading "N." (plus whatever whitespace postprocess
            # inserted: a space, a newline, etc.) so noun-phrase regexes match
            # on the bare English claim sentence.
            body = re.sub(
                rf'^{chunk.claim_num}\.\s*', '', chunk.translation, count=1
            )
            extracted_noun, extracted_actor = extract_preamble(
                body, chunk.claim_kind or "device", korean_source=chunk.text
            )
            noun = chunk.noun_phrase or extracted_noun
            actor = chunk.actor_phrase or extracted_actor
            chunk.noun_phrase = noun
            chunk.actor_phrase = actor
            preamble_specs[chunk.claim_num] = PreambleSpec(
                claim_num=chunk.claim_num,
                claim_kind=chunk.claim_kind or "device",
                noun_phrase=noun,
                actor_phrase=actor,
            )

        done += 1
        if done % 10 == 0 or done == total:
            progress(f"  CLAIM {done}/{total}")
        if delay > 0:
            time.sleep(delay)

    # ----- Phase 2: dependents in source order -------------------------------
    dependents = [c for c in valid if not c.is_independent]
    for chunk in dependents:
        parent_spec: PreambleSpec | None = None
        for p_num in chunk.parent_claim_nums:
            if p_num in preamble_specs:
                parent_spec = preamble_specs[p_num]
                break

        if parent_spec is None and verbose:
            print(
                f"  translate_claims claim {chunk.claim_num}: "
                f"no preamble for parents={chunk.parent_claim_nums}; falling back"
            )

        data = _translate_one(
            chunk, client=client, glossary=glossary,
            parent_spec=parent_spec, verbose=verbose,
        )
        if data is not None:
            merge_terms(glossary, data.get("key_terms") or [])

        if parent_spec is not None:
            chunk.noun_phrase = parent_spec.noun_phrase
            chunk.actor_phrase = parent_spec.actor_phrase
            preamble_specs[chunk.claim_num] = PreambleSpec(
                claim_num=chunk.claim_num,
                claim_kind=chunk.claim_kind or parent_spec.claim_kind,
                noun_phrase=parent_spec.noun_phrase,
                actor_phrase=parent_spec.actor_phrase,
            )

        done += 1
        if done % 10 == 0 or done == total:
            progress(f"  CLAIM {done}/{total}")
        if delay > 0:
            time.sleep(delay)

    return {"chunks_claims": chunks, "glossary": glossary}
