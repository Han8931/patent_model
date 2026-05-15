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

from ..docx_utils import _SEMICOLON_INDENT, _indent_after_colon, _normalize_unicode
from ..prompts import (
    build_claims_bulk_messages,
    parse_claims_bulk_glossary,
    parse_claims_bulk_response,
)
from ..state import Chunk, TranslationState


_HANGUL_RE = re.compile(r'[가-힯]')


def _contains_hangul(text: str | None) -> bool:
    return bool(text and _HANGUL_RE.search(text))


# --- Markdown stripping ----------------------------------------------------
# The bulk prompt is minimal, so the model occasionally returns markdown
# decorations that have no meaning once the text lands inside a docx <w:p>.
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_MD_BOLD_UNDERSCORE_RE = re.compile(r"__([^_\n]+?)__")
_MD_BULLET_LINE_RE = re.compile(r"^[ \t]*[-*+]\s+", flags=re.MULTILINE)


_MD_STRAY_BOLD_RE = re.compile(r"\*\*+")


def _strip_markdown(text: str) -> str:
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
    text = _MD_BULLET_LINE_RE.sub("", text)
    # Defensive: strip stray '**' the pair regex couldn't match (e.g. '**1.'
    # with no closing pair, or mismatched runs like '**A device.***').
    text = _MD_STRAY_BOLD_RE.sub("", text)
    return text


# --- Claim line-break normalization ---------------------------------------
# Two-step approach, idempotent end-to-end:
#
#   1. COLLAPSE phase — normalize every shape the model emits into ONE
#      canonical form. After collapse, every claim-element separator is
#      exactly "; " (semicolon + single space) and every list-opening colon
#      is exactly "<verb>: ". Newlines, tabs, double newlines, leading
#      indentation on continuation lines, and missing spaces all dissolve.
#
#   2. BREAK phase — insert the desired '\n\t' breaks against the now-uniform
#      input. Order matters: "; and " is matched first so the plain "; "
#      rule cannot consume its leading semicolon.
#
# This handles weirdness from the model that pure break-insertion couldn't
# catch:
#     "; \n and "   → "; and\n\t"
#     ";\n\n"       → ";\n\t" (no blank indented line)
#     ";a bar"      → ";\n\ta bar" (no-space case)
#     "\n  a foo;"  → "\n\ta foo;" (mixed leading whitespace stripped)
_COLLAPSE_SEMI_RE = re.compile(r";[ \t\n\r]*(?=\S)")
_COLLAPSE_COLON_VERB_RE = re.compile(
    r"\b(comprising|including|having|consisting(?:\s+(?:essentially\s+)?of)?)"
    r"[ \t]*:[ \t\n\r]*(?=\S)",
    re.IGNORECASE,
)

_BREAK_SEMI_AND_RE = re.compile(r";[ \t]+and\s+(?=\S)")
_BREAK_SEMI_RE = re.compile(r";[ \t]+(?!and\b)(?=\S)")
_BREAK_COLON_VERB_RE = re.compile(
    r"\b(comprising|including|having|consisting(?:\s+(?:essentially\s+)?of)?)"
    r"[ \t]*:[ \t]+(?=\S)",
    re.IGNORECASE,
)


def _normalize_claim_breaks(text: str) -> str:
    # Step 1: collapse every '; <whitespace>' shape to '; '.
    text = _COLLAPSE_SEMI_RE.sub("; ", text)
    text = _COLLAPSE_COLON_VERB_RE.sub(lambda m: f"{m.group(1)}: ", text)
    # Step 2: insert the canonical USPTO breaks.
    text = _BREAK_SEMI_AND_RE.sub(f"; and\n{_SEMICOLON_INDENT}", text)
    text = _BREAK_SEMI_RE.sub(f";\n{_SEMICOLON_INDENT}", text)
    text = _BREAK_COLON_VERB_RE.sub(
        lambda m: f"{m.group(1)}:\n{_SEMICOLON_INDENT}", text
    )
    return text


def _minimal_cleanup(claim_num: int, raw_text: str) -> str:
    """Strip markdown, normalize claim line breaks, normalize 'N. ' prefix."""
    text = _normalize_unicode(raw_text.strip())
    text = _strip_markdown(text)
    text = _normalize_claim_breaks(text)
    text = _indent_after_colon(text)
    # Normalize the leading "N." prefix so claim text always sits next to the
    # number with a single space — and the number always matches the chunk's
    # canonical claim_num. The bulk model sometimes echoes its own numbering
    # (e.g. starts every claim with "1." regardless of which claim it is); we
    # overwrite that with the real number from the chunk.
    #
    # Shapes handled:
    #   "1.\nA device"   → "<N>. A device"
    #   "1.A device"     → "<N>. A device"
    #   "1.   A device"  → "<N>. A device"
    #   "A device"       → "<N>. A device"  (prepended)
    leading_re = re.compile(r'^\s*\d+\s*\.\s*')
    m = leading_re.match(text)
    if m:
        text = f"{claim_num}. " + text[m.end():]
    else:
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
    glossary = dict(state.get("glossary", {}))

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
    claims_glossary = parse_claims_bulk_glossary(raw)
    if verbose:
        missing = [c.claim_num for c in valid if c.claim_num not in by_num]
        if missing:
            print(
                f"  translate_claims: bulk response missing claim(s): {missing}"
            )
        if claims_glossary:
            print(
                f"  translate_claims: seeded glossary with {len(claims_glossary)} "
                "term(s) from claim translations"
            )

    for chunk in valid:
        text = by_num.get(chunk.claim_num, "")
        if not text or _contains_hangul(text):
            chunk.translation = ""
            continue
        chunk.translation = _minimal_cleanup(chunk.claim_num, text)

    # Claim-derived terms SEED the glossary. The body translator extends it
    # later for description-only terms that claims don't name.
    for ko, en in claims_glossary.items():
        glossary.setdefault(ko, en)

    _assert_claims_translated(valid)
    return {"chunks_claims": chunks, "glossary": glossary}
