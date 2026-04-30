"""translate_claims — one LLM call per claim; track independent claim categories."""

from __future__ import annotations

import time

from ..docx_utils import postprocess
from ..glossary import extract_json_block, merge_terms
from ..prompts import build_claim_messages
from ..sections import CLAIM_ELEMENT_CAP_RE, LLM_CLAIM_PREFIX_RE
from ..state import TranslationState


def translate_claims(state: TranslationState) -> dict:
    chunks = state.get("chunks_claims", [])
    if not chunks:
        return {}

    client = state["client"]
    glossary = dict(state.get("glossary", {}))
    delay = state.get("delay", 0.0)
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)

    independent_categories: dict[int, str] = {}

    for chunk in chunks:
        if chunk.claim_num is None:
            continue
        progress(f"CLAIM {chunk.claim_num}")
        try:
            raw = client.complete(build_claim_messages(
                chunk.claim_num, chunk.text, glossary,
                independent_categories=independent_categories,
            ))
            data = extract_json_block(raw) or {}
            text = (data.get("text") or "").strip() or raw.strip()

            # Strip any LLM-produced claim-number prefix.
            text = LLM_CLAIM_PREFIX_RE.sub('', text.strip())
            # Lowercase first letter after :\n or ;\n.
            text = CLAIM_ELEMENT_CAP_RE.sub(lambda m: '\n' + m.group(1).lower(), text)
            # Prepend canonical "N. " number.
            text = f"{chunk.claim_num}. {text}"

            chunk.translation = postprocess(text)
            merge_terms(glossary, data.get("key_terms") or [])

            if data.get("is_independent"):
                cat = (data.get("category") or "").strip()
                if cat:
                    independent_categories[chunk.claim_num] = cat
        except Exception as exc:
            if verbose:
                print(f"  translate_claims claim {chunk.claim_num} failed: {exc}")
            chunk.translation = f"{chunk.claim_num}. {chunk.text}"

        if delay > 0:
            time.sleep(delay)

    return {"chunks_claims": chunks, "glossary": glossary}
