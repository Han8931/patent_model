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
    extract_preamble,
    method_dependent_connective,
)
from ..docx_utils import postprocess
from ..glossary import extract_json_block, merge_terms
from ..prompts import build_claim_messages
from ..sections import CLAIM_ELEMENT_CAP_RE, LLM_CLAIM_PREFIX_RE
from ..state import Chunk, TranslationState


def _format_translation(claim_num: int, raw_text: str) -> str:
    """Strip LLM-added claim numbers, lowercase after ';'/':',  prepend 'N. '."""
    text = LLM_CLAIM_PREFIX_RE.sub('', raw_text.strip())
    text = CLAIM_ELEMENT_CAP_RE.sub(lambda m: '\n' + m.group(1).lower(), text)
    return f"{claim_num}. {text}"


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
        )
        raw = client.complete(messages)
        data = extract_json_block(raw) or {}
        text = (data.get("text") or "").strip() or raw.strip()
        chunk.translation = postprocess(_format_translation(chunk.claim_num, text))
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
            noun, actor = extract_preamble(body, chunk.claim_kind or "device")
            chunk.noun_phrase = noun
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

        done += 1
        if done % 10 == 0 or done == total:
            progress(f"  CLAIM {done}/{total}")
        if delay > 0:
            time.sleep(delay)

    return {"chunks_claims": chunks, "glossary": glossary}
