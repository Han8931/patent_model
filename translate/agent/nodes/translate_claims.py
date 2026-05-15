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
# Markdown horizontal rule: a line containing only '---', '***', or '==='
# (3 or more). The model sometimes uses these to separate sections.
_MD_HR_RE = re.compile(
    r"^[ \t]*(?:-{3,}|\*{3,}|_{3,}|={3,})[ \t]*\n?", flags=re.MULTILINE
)


def _strip_markdown(text: str) -> str:
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
    text = _MD_BULLET_LINE_RE.sub("", text)
    text = _MD_HR_RE.sub("", text)
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
    # Normalize the leading claim-number prefix so the text always sits next
    # to the number with a single space — and the number always matches the
    # chunk's canonical claim_num. The bulk model uses every conceivable
    # numbering shape; we match them all and overwrite.
    #
    # Shapes handled:
    #   "1. A device"            "1.A device"
    #   "1.\nA device"           "1)   A device"
    #   "1: A device"            "Claim 1: A device"
    #   "Claim 1.\nA device"     "# 1. A device"
    #   "### Claim 1: A device"  "[Claim 1]"
    #
    # "A device" with no leading prefix gets "<N>. " prepended.
    m = _LEADING_CLAIM_PREFIX_RE.match(text)
    if m:
        text = f"{claim_num}. " + text[m.end():]
    else:
        text = f"{claim_num}. {text}"
    return text


_LEADING_CLAIM_PREFIX_RE = re.compile(
    r'^'
    r'\s*'                       # leading whitespace
    r'\[?'                       # optional opening bracket
    r'(?:#{1,6}\s*)?'            # optional markdown heading marks
    r'(?:claim\s+)?'             # optional "Claim " word
    r'\d+'                       # the number itself
    r'\s*[.:)\]]?'               # optional separator: '.', ':', ')', ']'
    r'\s*',                      # trailing whitespace before claim text
    flags=re.IGNORECASE,
)


def _assert_claims_translated(chunks: list[Chunk]) -> None:
    """Backwards-compatible alias used by the review node after revision.

    Previously this raised RuntimeError on any failed claim, killing the run.
    Now it only logs a warning — partial Korean in the claims section is
    handled the same way it is for body / abstract: the writer leaves those
    paragraphs untouched and the user gets a heads-up at the end.
    """
    def _print(msg: str) -> None:
        print(msg, flush=True)
    _report_claim_failures(chunks, _print)


def _report_claim_failures(chunks: list[Chunk], progress) -> list[int]:
    """Return the list of claim_nums still missing or still Korean.

    The old behavior was to raise RuntimeError on any failure, which killed
    the whole run when even one claim came back garbled. That's too strict —
    body and abstract just leave the source paragraph untouched and keep
    going. Match that here: log a clear warning so the user knows which
    claims to inspect, but let the writer continue with whatever translated
    claims we got.
    """
    failed = [
        c.claim_num
        for c in chunks
        if c.claim_num is not None
        and (not c.translation or _contains_hangul(c.translation))
    ]
    if failed:
        progress(
            "WARNING: CLAIMS translation unavailable for claim(s): "
            + ", ".join(str(n) for n in failed)
            + ". Those claim paragraphs will remain unchanged unless the final "
              "Korean-ratio check aborts the file."
        )
    return failed


def _run_bulk_claims(
    client,
    pairs: list[tuple[int, str]],
    *,
    retry_problem: str | None = None,
) -> tuple[dict[int, str], dict[str, str]]:
    """One bulk LLM call → ({claim_num: english}, {ko: en}).

    When ``retry_problem`` is set, an extra corrective user message is
    appended to the standard bulk prompt — used for the retry pass.
    """
    messages = build_claims_bulk_messages(pairs)
    if retry_problem:
        messages = messages + [{
            "role": "user",
            "content": (
                "Your previous response was unusable. "
                f"Problem: {retry_problem}. "
                "Translate the Korean claims above into English now. "
                "Output English only — no Korean characters anywhere, "
                "no markdown, no commentary. "
                "Return each claim under its '===== CLAIM N =====' banner. "
                "Keep every [EQUATION_N] marker verbatim and in the same "
                "relative position."
            ),
        }]
    raw = client.complete(messages)
    return parse_claims_bulk_response(raw), parse_claims_bulk_glossary(raw)


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
    try:
        by_num, claims_glossary = _run_bulk_claims(client, pairs)
    except Exception as exc:
        progress(
            f"WARNING: bulk claim translation call failed: "
            f"{type(exc).__name__}: {exc}; leaving claim paragraphs untouched"
        )
        return {"chunks_claims": chunks, "glossary": glossary}

    # First pass: apply the cleaned text to every chunk that came back with
    # usable English.
    for chunk in valid:
        text = by_num.get(chunk.claim_num, "")
        if text and not _contains_hangul(text):
            chunk.translation = _minimal_cleanup(chunk.claim_num, text)
        else:
            chunk.translation = ""

    # Retry pass: re-send just the failing claims with a corrective message.
    still_failing = [c for c in valid if not c.translation]
    if still_failing:
        problem = (
            "some claims were missing or still contained Korean characters"
        )
        if verbose:
            print(
                f"  translate_claims: retrying {len(still_failing)} claim(s): "
                f"{[c.claim_num for c in still_failing]}"
            )
        try:
            retry_pairs = [(c.claim_num, c.text) for c in still_failing]
            retry_by_num, retry_glossary = _run_bulk_claims(
                client, retry_pairs, retry_problem=problem
            )
            for c in still_failing:
                text = retry_by_num.get(c.claim_num, "")
                if text and not _contains_hangul(text):
                    c.translation = _minimal_cleanup(c.claim_num, text)
            # Merge any additional glossary entries the retry produced.
            for ko, en in retry_glossary.items():
                claims_glossary.setdefault(ko, en)
        except Exception as exc:
            if verbose:
                print(
                    f"  translate_claims: retry call failed: "
                    f"{type(exc).__name__}: {exc}"
                )

    if verbose and claims_glossary:
        print(
            f"  translate_claims: seeded glossary with {len(claims_glossary)} "
            "term(s) from claim translations"
        )

    # Claim-derived terms SEED the glossary. The body translator extends it
    # later for description-only terms that claims don't name.
    for ko, en in claims_glossary.items():
        glossary.setdefault(ko, en)

    _report_claim_failures(valid, progress)
    return {"chunks_claims": chunks, "glossary": glossary}
