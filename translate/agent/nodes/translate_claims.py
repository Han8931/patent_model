"""translate_claims — single bundled LLM call for ALL claims.

Sends every claim's Korean text to the model in one user message, banner-
delimited by claim number. The model returns one English block per banner;
we split the response and assign each piece to its chunk.

Image-based equations are represented as [EQUATION_N] markers in the input
(produced by chunk_claims). The bulk prompt requires those markers to come
back verbatim so the writer can splice the original Word equation XML back
into the translated claim paragraphs.

Postprocess is intentionally minimal — only Unicode normalization runs.
Earlier deterministic sweeps (further-comprising rewrite, article fixes,
reference-paren stripping, etc.) have been turned off so this path can be
tested against the model's raw drafting behavior.
"""

from __future__ import annotations

import re

from ..docx_utils import _normalize_unicode
from ..prompts import build_claims_bulk_messages, parse_claims_bulk_response
from ..state import Chunk, TranslationState


_HANGUL_RE = re.compile(r'[가-힯]')


def _contains_hangul(text: str | None) -> bool:
    return bool(text and _HANGUL_RE.search(text))


def _minimal_cleanup(claim_num: int, raw_text: str) -> str:
    """Unicode-normalize and prepend 'N. ' if the model didn't include it."""
    text = _normalize_unicode(raw_text.strip())
    if not re.match(rf'^\s*{claim_num}\s*\.\s', text):
        text = f"{claim_num}. {text}"
    return text


def _assert_claims_translated(chunks: list[Chunk]) -> None:
    failed = [
        str(c.claim_num)
        for c in chunks
        if c.claim_num is not None
        and (not c.translation or _contains_hangul(c.translation))
    ]
    if failed:
        raise RuntimeError(
            "Claim translation failed for claim(s): "
            + ", ".join(failed)
            + ". Refusing to write a partially Korean claims section."
        )


def translate_claims(state: TranslationState) -> dict:
    chunks: list[Chunk] = state.get("chunks_claims", [])
    if not chunks:
        return {}

    client = state["client"]
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)

    valid = [c for c in chunks if c.claim_num is not None]
    total = len(valid)
    if total == 0:
        return {"chunks_claims": chunks}

    progress(f"Translating CLAIMS ({total} claims, one bundled call)…")

    pairs = [(c.claim_num, c.text) for c in valid]
    messages = build_claims_bulk_messages(pairs)

    try:
        raw = client.complete(messages)
    except Exception as exc:
        raise RuntimeError(
            f"Bulk claim translation call failed: {type(exc).__name__}: {exc}"
        ) from exc

    by_num = parse_claims_bulk_response(raw)
    if verbose:
        missing = [c.claim_num for c in valid if c.claim_num not in by_num]
        if missing:
            print(
                f"  translate_claims: bulk response missing claim(s): {missing}"
            )

    for chunk in valid:
        text = by_num.get(chunk.claim_num, "")
        if not text or _contains_hangul(text):
            chunk.translation = ""
            continue
        chunk.translation = _minimal_cleanup(chunk.claim_num, text)

    _assert_claims_translated(valid)
    return {"chunks_claims": chunks}
