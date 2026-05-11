"""plan_claim_preambles — LLM pre-pass for independent claim preambles."""

from __future__ import annotations

import re
import time

from ..glossary import clean_text, extract_json_block
from ..prompts import build_preamble_plan_messages
from ..state import Chunk, TranslationState


_HANGUL_RE = re.compile(r'[가-힯]')


def _valid_english(value: str) -> bool:
    return bool(value) and not _HANGUL_RE.search(value)


def _apply_plan(chunk: Chunk, data: dict, verbose: bool) -> None:
    noun = clean_text(data.get("english_noun_phrase"))
    preamble = clean_text(data.get("independent_preamble"))
    actor = clean_text(data.get("actor_phrase"))
    subject = clean_text(data.get("korean_subject_span"))

    if noun and _valid_english(noun):
        chunk.noun_phrase = noun
    if preamble and _valid_english(preamble):
        chunk.independent_preamble = preamble
    if actor and _valid_english(actor):
        chunk.actor_phrase = actor
    if subject:
        chunk.korean_subject_span = subject

    if verbose and not chunk.independent_preamble:
        print(
            f"  plan_claim_preambles claim {chunk.claim_num}: "
            "no usable independent_preamble"
        )


def plan_claim_preambles(state: TranslationState) -> dict:
    chunks: list[Chunk] = state.get("chunks_claims", [])
    independents = sorted(
        (c for c in chunks if c.claim_num is not None and c.is_independent),
        key=lambda c: c.claim_num or 0,
    )
    if not independents:
        return {}

    client = state["client"]
    glossary = dict(state.get("glossary", {}))
    delay = state.get("delay", 0.0)
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)

    progress(f"Planning CLAIM preambles ({len(independents)} independent claims)…")

    for i, chunk in enumerate(independents, 1):
        try:
            raw = client.complete(
                build_preamble_plan_messages(
                    claim_num=chunk.claim_num,
                    chunk_text=chunk.text,
                    kind=chunk.claim_kind or "device",
                    glossary=glossary,
                )
            )
            data = extract_json_block(raw) or {}
            if isinstance(data, dict):
                _apply_plan(chunk, data, verbose)
        except Exception as exc:
            if verbose:
                print(
                    f"  plan_claim_preambles claim {chunk.claim_num} failed: {exc}"
                )

        if i % 10 == 0 or i == len(independents):
            progress(f"  PREAMBLE {i}/{len(independents)}")
        if delay > 0:
            time.sleep(delay)

    return {"chunks_claims": chunks}
