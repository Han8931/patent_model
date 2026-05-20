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
from pathlib import Path

from ..claim_classifier import (
    PreambleSpec,
    build_dependent_preamble,
    dependent_adds_subject_matter,
    extract_preamble,
)
from ..docx_utils import _SEMICOLON_INDENT, _indent_after_colon, _normalize_unicode
from ..prompts import (
    build_claim_retry_messages,
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


def _snippet(text: str, width: int = 80) -> str:
    """One-line snippet for log/error output."""
    flat = (text or "").replace("\n", " ⏎ ").replace("\t", " ⇥ ")
    if len(flat) <= width:
        return flat
    return flat[: width - 1] + "…"


def _hangul_count(text: str) -> int:
    return sum(1 for c in (text or "") if _HANGUL_RE.fullmatch(c))


_EQUATION_MARKER_RE = re.compile(r"\[EQUATION_\d+\]")
_BRACKETED_HANGUL_RE = re.compile(r"[\[(（【][^\])）】\n]*[가-힯][^\])）】\n]*[\])）】]")
_LEADING_NUM_RE = re.compile(r"^\s*\d+\s*[.)]\s*")
_DEPENDENT_PREFIX_RE = re.compile(
    r"^The\s+.+?\s+of\s+(?:claim\s+\d+(?:\s+or\s+claim\s+\d+)?|"
    r"any\s+one\s+of\s+claims\s+\d+\s+to\s+\d+),\s*"
    r"(?P<conn>wherein|further\s+comprising)\s+",
    re.IGNORECASE | re.DOTALL,
)
_INDEPENDENT_PREFIX_RE = re.compile(
    r"^A[n]?\s+.+?\s+(?:comprising|including|having)\s*:\s*",
    re.IGNORECASE | re.DOTALL,
)
_ADDITION_VERB_RE = re.compile(
    r"^(?:wherein\s+)?(?:the\s+.+?\s+)?(?:further\s+)?"
    r"(?:comprises|comprising|includes|including|has|having)\s+",
    re.IGNORECASE | re.DOTALL,
)


def _lower_initial(text: str) -> str:
    if not text:
        return text
    return text[0].lower() + text[1:] if text[0].isupper() else text


def _strip_terminal_period(text: str) -> str:
    return re.sub(r"\s*\.\s*$", "", text.strip())


def _extract_dependent_tail(text: str, *, source_adds: bool) -> str:
    body = _LEADING_NUM_RE.sub("", text.strip())
    body = _DEPENDENT_PREFIX_RE.sub("", body, count=1)
    body = _INDEPENDENT_PREFIX_RE.sub("", body, count=1)
    body = re.sub(r"^(?:wherein|where)\s+", "", body, flags=re.IGNORECASE)
    if source_adds:
        body = _ADDITION_VERB_RE.sub("", body, count=1)
    return _lower_initial(_strip_terminal_period(body))


def _ensure_terminal_period(text: str) -> str:
    text = text.rstrip()
    return text if text.endswith(".") else text + "."


def _claim_validation_problem(source: str, translation: str) -> str | None:
    if not translation.strip():
        return "missing translation"
    if _contains_hangul(translation):
        return "Korean/Hangul left in translation"

    source_markers = set(_EQUATION_MARKER_RE.findall(source))
    missing_markers = sorted(m for m in source_markers if m not in translation)
    if missing_markers:
        return f"missing equation marker(s): {', '.join(missing_markers)}"

    source_bracketed = _BRACKETED_HANGUL_RE.findall(source)
    if source_bracketed:
        translated_bracketed = re.findall(r"[\[(（【][^\])）】\n]{2,}[\])）】]", translation)
        if len(translated_bracketed) < len(source_bracketed):
            return "bracketed Korean content appears omitted"

    source_units = [
        p.strip()
        for p in re.split(r"[;；。\n]+|\s+및\s+|\s+또는\s+", source)
        if p.strip() and not _EQUATION_MARKER_RE.fullmatch(p.strip())
    ]
    if len(source_units) >= 4:
        english_units = [
            p.strip()
            for p in re.split(r"[;\n]+|\s+\band\b\s+|\s+\bor\b\s+", translation)
            if p.strip()
        ]
        if len(english_units) < max(2, len(source_units) - 2):
            return "translation is suspiciously shorter than the source claim"

    return None


def _repair_dependent_claims(chunks: list[Chunk]) -> None:
    by_num = {c.claim_num: c for c in chunks if c.claim_num is not None}
    preambles: dict[int, PreambleSpec] = {}

    for chunk in sorted(by_num.values(), key=lambda c: c.claim_num or 0):
        if not chunk.translation or chunk.is_independent is False:
            continue
        kind = chunk.claim_kind or "device"
        noun, actor = extract_preamble(
            _LEADING_NUM_RE.sub("", chunk.translation),
            kind,
            korean_source=chunk.text,
        )
        chunk.noun_phrase = noun
        chunk.actor_phrase = actor
        preambles[chunk.claim_num or 0] = PreambleSpec(
            claim_num=chunk.claim_num or 0,
            claim_kind=kind,
            noun_phrase=noun,
            actor_phrase=actor,
        )

    for chunk in sorted(by_num.values(), key=lambda c: c.claim_num or 0):
        if not chunk.translation or chunk.is_independent is not False:
            continue
        if not chunk.parent_claim_nums:
            continue
        parent = preambles.get(chunk.parent_claim_nums[0])
        if parent is None:
            continue
        source_adds = dependent_adds_subject_matter(chunk.text)
        connective = "further comprising" if source_adds else "wherein"
        prefix = build_dependent_preamble(
            parent,
            chunk.parent_claim_nums,
            chunk.multi_parent_kind,
        )
        tail = _extract_dependent_tail(chunk.translation, source_adds=source_adds)
        if not tail:
            continue
        chunk.translation = _normalize_claim_breaks(
            _ensure_terminal_period(
                f"{chunk.claim_num}. {prefix}, {connective} {tail}"
            )
        )


def _assert_claims_translated(chunks: list[Chunk]) -> None:
    """Hard guard used by the review node after applying revisions.

    Lists each failing claim with a reason (MISSING vs HANGUL leftover) so
    the user sees what went wrong even when the failure happens during
    review rather than initial translation.
    """
    failed: list[tuple[int, str]] = []
    for c in chunks:
        if c.claim_num is None:
            continue
        if not c.translation:
            failed.append((c.claim_num, "MISSING translation"))
            continue
        problem = _claim_validation_problem(c.text, c.translation)
        if problem:
            if _contains_hangul(c.translation):
                n_ko = _hangul_count(c.translation)
                problem = f"HANGUL leftover ({n_ko} chars)"
            failed.append((c.claim_num, problem))
    if failed:
        details = "\n".join(f"  claim {num}: {reason}" for num, reason in failed)
        raise RuntimeError(
            "Claim translation failed for "
            f"{len(failed)} claim(s). "
            "Refusing to write a partially Korean claims section.\n"
            f"{details}"
        )


def _dump_bulk_artifacts(
    output_path: Path | None,
    request_user_msg: str,
    raw_response: str,
) -> tuple[Path | None, Path | None]:
    """Write the bulk request/response next to the docx output for post-mortem.

    Returns ``(req_path, resp_path)`` or ``(None, None)`` if writing failed.
    Logging must never become the reason a translation fails, so OSError is
    swallowed.
    """
    if output_path is None:
        return None, None
    stem = output_path.with_suffix("")
    req_path = Path(f"{stem}.claims_bulk.req.txt")
    resp_path = Path(f"{stem}.claims_bulk.resp.txt")
    try:
        req_path.parent.mkdir(parents=True, exist_ok=True)
        req_path.write_text(request_user_msg, encoding="utf-8")
        resp_path.write_text(raw_response, encoding="utf-8")
        return req_path, resp_path
    except OSError:
        return None, None


def translate_claims(state: TranslationState) -> dict:
    chunks: list[Chunk] = state.get("chunks_claims", [])
    if not chunks:
        return {}

    client = state["client"]
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)
    glossary = dict(state.get("glossary", {}))
    output_path = state.get("output_path")

    valid = [c for c in chunks if c.claim_num is not None]
    total = len(valid)
    if total == 0:
        return {"chunks_claims": chunks}

    progress(f"Translating CLAIMS ({total} claims, one bundled call)…")

    pairs = [(c.claim_num, c.text) for c in valid]
    messages = build_claims_bulk_messages(pairs)
    request_user_msg = next(
        (m["content"] for m in messages if m.get("role") == "user"), ""
    )

    try:
        raw = client.complete(messages)
    except Exception as exc:
        # Dump the request so the user can see what we tried to send even
        # when the API call itself errored before any response came back.
        req_path, _ = _dump_bulk_artifacts(output_path, request_user_msg, "")
        suffix = f" (request dumped to {req_path})" if req_path else ""
        raise RuntimeError(
            f"Bulk claim translation call failed: {type(exc).__name__}: {exc}{suffix}"
        ) from exc

    by_num = parse_claims_bulk_response(raw)
    claims_glossary = parse_claims_bulk_glossary(raw)

    # Always-on bulk summary — surfaces truncation/banner-drift without
    # needing --verbose. Goes through `progress` so it also lands in the log.
    expected = {c.claim_num for c in valid}
    parsed = set(by_num.keys())
    missing_nums = sorted(expected - parsed)
    extra_nums = sorted(parsed - expected)
    progress(
        f"  claims bulk: {total} sent · {len(parsed)} banners parsed · "
        f"{len(claims_glossary)} glossary terms · raw={len(raw)} chars"
    )
    if missing_nums:
        progress(f"  claims bulk: MISSING banner for claim(s): {missing_nums}")
    if extra_nums:
        progress(f"  claims bulk: UNEXPECTED claim banner(s) in response: {extra_nums}")

    # Per-claim status — verbose only.
    failed: list[tuple[int, str, str]] = []  # (claim_num, reason, snippet)
    for chunk in valid:
        num = chunk.claim_num
        text = by_num.get(num, "")
        if not text:
            chunk.translation = ""
            failed.append((num, "MISSING banner", ""))
            if verbose:
                print(f"  claim {num:>3}: MISSING banner in bulk response")
            continue
        n_ko = _hangul_count(text)
        if n_ko:
            chunk.translation = ""
            failed.append((num, f"HANGUL leftover ({n_ko} chars)", _snippet(text)))
            if verbose:
                print(
                    f"  claim {num:>3}: HANGUL leftover "
                    f"({n_ko}/{len(text)} chars) — '{_snippet(text, 60)}'"
            )
            continue
        cleaned = _minimal_cleanup(num, text)
        problem = _claim_validation_problem(chunk.text, cleaned)
        if problem:
            if verbose:
                print(f"  claim {num:>3}: retrying ({problem})")
            try:
                retry_raw = client.complete(
                    build_claim_retry_messages(num, chunk.text, problem)
                )
                retry_by_num = parse_claims_bulk_response(retry_raw)
                retry_text = retry_by_num.get(num) or retry_raw
                retry_cleaned = _minimal_cleanup(num, retry_text)
                retry_problem = _claim_validation_problem(chunk.text, retry_cleaned)
                if retry_problem:
                    chunk.translation = ""
                    failed.append((num, retry_problem, _snippet(retry_cleaned)))
                    if verbose:
                        print(
                            f"  claim {num:>3}: retry unusable "
                            f"({retry_problem}) — '{_snippet(retry_cleaned, 60)}'"
                        )
                    continue
                cleaned = retry_cleaned
            except Exception as exc:
                chunk.translation = ""
                failed.append((num, f"retry failed ({type(exc).__name__}: {exc})", ""))
                continue
        chunk.translation = cleaned
        if verbose:
            print(f"  claim {num:>3}: OK ({len(cleaned)} chars)")

    _repair_dependent_claims(valid)

    if verbose and claims_glossary:
        print(
            f"  translate_claims: seeded glossary with {len(claims_glossary)} "
            "term(s) from claim translations"
        )

    # Claim-derived terms SEED the glossary. The body translator extends it
    # later for description-only terms that claims don't name.
    for ko, en in claims_glossary.items():
        glossary.setdefault(ko, en)

    if failed:
        req_path, resp_path = _dump_bulk_artifacts(
            output_path, request_user_msg, raw
        )
        details = "\n".join(
            f"  claim {num}: {reason}"
            + (f" — '{snip}'" if snip else "")
            for num, reason, snip in failed
        )
        artifact_lines = []
        if req_path:
            artifact_lines.append(f"  request : {req_path}")
        if resp_path:
            artifact_lines.append(f"  response: {resp_path}")
        artifact_block = (
            "\nArtifacts:\n" + "\n".join(artifact_lines) if artifact_lines else ""
        )
        raise RuntimeError(
            f"Claim translation failed for {len(failed)} claim(s) "
            f"of {total}. Refusing to write a partially Korean claims section.\n"
            f"{details}{artifact_block}"
        )

    _assert_claims_translated(chunks)
    return {"chunks_claims": chunks, "glossary": glossary}
