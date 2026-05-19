"""End-to-end translation pipeline for a single .docx.

Public surface (used by ``main.py`` and ``batch.py``):
  * :func:`translate_file` — the full pipeline.
  * :func:`resolve_output_path` — CLI path/suffix helper.

The pipeline:

  1. claims    — bulk call → validate against the Korean 【청구항 N】 anchors
                 → retry any missing claim individually → review pass.
                 Returns claims + a Korean→English glossary.
  2. abstract  — single call, using the claims glossary.
  3. description — paragraph-by-paragraph with a rolling window of recent
                   (Korean, English) pairs for continuity, all sharing the
                   same glossary.

Failures are logged to ``<out_path>.log`` and the pipeline continues so one
bad LLM call doesn't take down the whole file.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from pathlib import Path
from typing import Callable

from docx import Document

from .client import ClientConfig, LLMClient
from .docx_io import (
    apply_abstract,
    apply_claims,
    apply_output_font,
    apply_section_headers,
    CLAIM_HEADER_RE,
    collect_block,
    scan_paragraphs,
    set_paragraph_text,
)
from .prompts import (
    ABSTRACT_PROMPT,
    CLAIM_RETRY_PROMPT,
    CLAIMS_MAX_TOKENS,
    CLAIMS_PROMPT,
    CLAIMS_REVIEW_PROMPT,
    DESCRIPTION_CONTEXT_WINDOW,
    DESCRIPTION_PROMPT,
)


Progress = Callable[[str], None]


# ---------------------------------------------------------------------------
# Prompt-building helpers (Korean source + glossary + rolling context)
# ---------------------------------------------------------------------------

def _glossary_block(glossary: dict[str, str]) -> str:
    if not glossary:
        return ""
    lines = [f"  {kr}  →  {en}" for kr, en in glossary.items()]
    return "Glossary (use these exact English terms):\n" + "\n".join(lines) + "\n\n"


def _context_block(pairs) -> str:
    """Format the rolling (Korean, English) pairs as 'already translated' context.

    Whitespace inside each paragraph is collapsed to keep the prompt compact;
    the LLM doesn't need the original line layout to use the pair for continuity.
    """
    if not pairs:
        return ""
    lines = [
        "Recent context (already translated; do NOT retranslate — for continuity only):"
    ]
    for kr, en in pairs:
        kr_one = re.sub(r"\s+", " ", kr).strip()
        en_one = re.sub(r"\s+", " ", en).strip()
        lines.append(f"- Korean:  {kr_one}")
        lines.append(f"  English: {en_one}")
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Claims response parser (plain-text + ---GLOSSARY--- sentinel)
# ---------------------------------------------------------------------------

_GLOSSARY_SENTINEL_RE = re.compile(r"^\s*-{2,}\s*GLOSSARY\s*-{2,}\s*$", re.MULTILINE)
_CLAIM_LINE_RE = re.compile(r"^(\d+)\.\s")
_GLOSSARY_LINE_RE = re.compile(r"^\s*(.+?)\s*(?:->|→|=>|:)\s*(.+?)\s*$")


def _parse_claims_response(raw: str) -> tuple[dict[str, str], dict[str, str]]:
    """Parse a plain-text claims block followed by an optional ---GLOSSARY--- section."""
    text = raw.strip()
    # Strip ``` fences if the model added them anyway.
    fence = re.match(r"```[a-zA-Z]*\s*(.*?)\s*```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    parts = _GLOSSARY_SENTINEL_RE.split(text, maxsplit=1)
    claims_block = parts[0].strip()
    glossary_block = parts[1].strip() if len(parts) > 1 else ""

    claims: dict[str, str] = {}
    current_num: str | None = None
    current_lines: list[str] = []
    for line in claims_block.splitlines():
        m = _CLAIM_LINE_RE.match(line)
        if m:
            if current_num is not None:
                claims[current_num] = "\n".join(current_lines).rstrip()
            current_num = m.group(1)
            current_lines = [line]
        elif current_num is not None:
            current_lines.append(line)
    if current_num is not None:
        claims[current_num] = "\n".join(current_lines).rstrip()

    glossary: dict[str, str] = {}
    for line in glossary_block.splitlines():
        m = _GLOSSARY_LINE_RE.match(line)
        if m:
            glossary[m.group(1).strip()] = m.group(2).strip()

    return claims, glossary


def _claim_korean_by_num(records: list[dict]) -> dict[str, str]:
    """Group claim paragraphs by 【청구항 N】 anchor → joined Korean text."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for r in records:
        if r["role"] != "claim":
            continue
        m = CLAIM_HEADER_RE.match(r["text"])
        if m:
            current = m.group(1)
            out.setdefault(current, []).append(r["text"])
        elif current is not None:
            out.setdefault(current, []).append(r["text"])
    return {k: "\n".join(v) for k, v in out.items()}


def _format_claims_block(claims: dict[str, str], order: list[str]) -> str:
    """Re-emit claims as a plain-text block in the given numeric order."""
    parts = [claims[n] for n in order if n in claims and claims[n].strip()]
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Section translation calls
# ---------------------------------------------------------------------------

def _retranslate_single_claim(
    client: LLMClient,
    num: str,
    korean: str,
    prior_english: str,
    glossary: dict[str, str],
    *,
    claim_type: str | None = None,
) -> str:
    user_parts: list[str] = []
    if glossary:
        user_parts.append(_glossary_block(glossary))
    if prior_english:
        user_parts.append("Previously translated claims (for terminology):")
        user_parts.append(prior_english)
        user_parts.append("")
    if claim_type:
        user_parts.append(f"Claim {num} type: {claim_type}")
        user_parts.append("")
    user_parts.append(f"Korean claim {num}:")
    user_parts.append(korean)
    return client.complete(
        [
            {"role": "system", "content": CLAIM_RETRY_PROMPT.format(N=num)},
            {"role": "user",   "content": "\n".join(user_parts)},
        ],
        max_tokens=CLAIMS_MAX_TOKENS,
    ).strip()


def _review_claims(
    client: LLMClient,
    korean_block: str,
    english_block: str,
    glossary: dict[str, str],
) -> dict[str, str]:
    """Run a review pass; return any corrected claims (may be empty)."""
    user_parts: list[str] = []
    if glossary:
        user_parts.append(_glossary_block(glossary))
    user_parts.append("Korean claims:")
    user_parts.append(korean_block)
    user_parts.append("")
    user_parts.append("Current English translation:")
    user_parts.append(english_block)
    raw = client.complete(
        [
            {"role": "system", "content": CLAIMS_REVIEW_PROMPT},
            {"role": "user",   "content": "\n".join(user_parts)},
        ],
        max_tokens=CLAIMS_MAX_TOKENS,
    )
    revised, _ = _parse_claims_response(raw)
    return revised


def translate_claims(
    client: LLMClient,
    korean_block: str,
    records: list[dict],
    *,
    progress: Progress = print,
    logger: logging.Logger | None = None,
    claim_types: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Returns (claim_num → English claim text, glossary).

    Strategy:
      1. Bulk-translate all claims in one call (best for terminology consistency).
      2. Validate: every 【청구항 N】 anchor in the source must have a non-empty
         English claim. Retry missing ones individually with the rest as context.
      3. Run a review pass on the assembled English block; apply any corrections.

    *claim_types* (optional) is a ``{claim_num: "INDEPENDENT" | "DEPENDENT, parent=N"}``
    map from the agentic classifier. When provided, the types are prepended to
    the bulk + retry user messages so the LLM can pick the right preamble form
    up front instead of inferring it.
    """
    type_block = ""
    if claim_types:
        type_lines = [
            f"- Claim {n}: {claim_types[n]}"
            for n in sorted(claim_types, key=lambda s: int(s))
        ]
        type_block = (
            "Claim types (use these to pick the right preamble form):\n"
            + "\n".join(type_lines)
            + "\n\n"
        )

    # --- 1) bulk translate ---------------------------------------------------
    raw = client.complete(
        [
            {"role": "system", "content": CLAIMS_PROMPT},
            {"role": "user",   "content": type_block + korean_block},
        ],
        max_tokens=CLAIMS_MAX_TOKENS,
    )
    claims, glossary = _parse_claims_response(raw)

    # --- 2) validate against the Korean anchors, retry missing ---------------
    korean_by_num = _claim_korean_by_num(records)
    expected = sorted(korean_by_num.keys(), key=int) if korean_by_num else \
               sorted(claims.keys(), key=lambda s: int(s))

    missing = [n for n in expected if not claims.get(n, "").strip()]
    if missing:
        progress(f"      bulk pass missed {len(missing)} claim(s) {missing}; retrying individually…")
        if logger:
            logger.warning(f"bulk claims pass missed {len(missing)} claim(s): {missing}")
        prior = _format_claims_block(claims, [n for n in expected if n not in missing])
        for n in missing:
            kr = korean_by_num.get(n)
            if not kr:
                continue
            try:
                claims[n] = _retranslate_single_claim(
                    client, n, kr, prior, glossary,
                    claim_type=(claim_types.get(n) if claim_types else None),
                )
            except Exception as e:
                progress(f"      retry of claim {n} failed: {e!r}")
                if logger:
                    logger.exception(f"retry of claim {n} failed")
                continue
            # extend prior context so each retry sees the previous retries too
            prior = (prior + "\n\n" + claims[n]).strip()

    # --- 3) review pass ------------------------------------------------------
    english_block = _format_claims_block(claims, expected)
    if english_block:
        progress("      running review pass…")
        try:
            revised = _review_claims(client, korean_block, english_block, glossary)
        except Exception as e:
            progress(f"      review failed — skipping: {e!r}")
            if logger:
                logger.exception("review pass failed")
            revised = {}
        applied = 0
        for n, text in revised.items():
            if n in expected and text.strip() and text.strip() != claims.get(n, "").strip():
                claims[n] = text
                applied += 1
        if applied:
            progress(f"      review revised {applied} claim(s)")
            if logger:
                logger.info(f"review revised {applied} claim(s)")
        else:
            progress("      review found no changes")

    return claims, glossary


def translate_abstract(client: LLMClient, korean: str, glossary: dict[str, str]) -> str:
    user = _glossary_block(glossary) + "Korean abstract:\n" + korean
    return client.complete([
        {"role": "system", "content": ABSTRACT_PROMPT},
        {"role": "user",   "content": user},
    ]).strip()


def translate_paragraph(
    client: LLMClient,
    korean: str,
    glossary: dict[str, str],
    *,
    context=None,
) -> str:
    """Translate one description paragraph.

    *context* is an iterable of ``(korean, english)`` pairs from the immediately
    preceding paragraphs. The LLM sees them as read-only continuity, not as
    targets to retranslate. Pass ``None`` (or an empty iterable) on the first
    paragraph; the loop in :func:`translate_file` grows the window from there.
    """
    parts: list[str] = []
    if glossary:
        parts.append(_glossary_block(glossary))
    if context:
        parts.append(_context_block(context))
    parts.append("Korean paragraph to translate:\n" + korean)
    return client.complete([
        {"role": "system", "content": DESCRIPTION_PROMPT},
        {"role": "user",   "content": "".join(parts)},
    ]).strip()


# ---------------------------------------------------------------------------
# Per-file logger (instantiated directly so concurrent threads don't share)
# ---------------------------------------------------------------------------

def _setup_logger(in_path: Path, log_path: Path) -> logging.Logger:
    """Per-file logger that writes to *log_path* and is independent of other calls.

    We instantiate ``Logger`` directly (instead of ``getLogger``) so concurrent
    translations in batch mode don't share handlers via the global registry.
    """
    logger = logging.Logger(f"translate.{in_path.stem}")
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    return logger


def _teardown_logger(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def resolve_output_path(in_path: Path, output: Path | None, suffix: str) -> Path:
    """Decide where to write the English .docx given the CLI args.

    Default: ``output/<stem>_en.docx``. If *suffix* is non-empty it's appended
    to the stem (so ``--suffix _v1`` → ``output/<stem>_en_v1.docx``). Suffix
    is also applied when *output* is given, so explicit names track versions
    the same way.
    """
    out_path = output if output else Path("output") / f"{in_path.stem}_en.docx"
    if suffix:
        out_path = out_path.with_stem(out_path.stem + suffix)
    return out_path


def translate_file(
    in_path: Path,
    out_path: Path,
    *,
    progress: Progress = print,
    model: str | None = None,
) -> None:
    """Translate one .docx end-to-end. Used by both the CLI and ``batch.py``.

    Errors are logged to ``<out_path>.log`` (overwriting per run). Failures in
    a single description paragraph leave the Korean text in place rather than
    aborting the whole file; the same is true for total claims or abstract
    failures — they're logged and the pipeline continues.

    *model* overrides ``LLM_MODEL`` from ``.env`` for this run only; the rest
    of the LLM config (base URL, API key, temperature, max_tokens) still comes
    from the environment.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = out_path.with_suffix(".log")
    logger = _setup_logger(in_path, log_path)
    logger.info(f"translate {in_path} -> {out_path}")

    try:
        config = ClientConfig.from_env()
        if model:
            config.model = model
        logger.info(f"model={config.model} base_url={config.base_url}")
        client = LLMClient(config)
        doc = Document(in_path)
        records = scan_paragraphs(doc)

        media = [r for r in records if r["role"] == "media"]
        mixed_media = [r for r in media if r["text"].strip()]
        if media:
            logger.info(
                f"{len(media)} paragraph(s) contain inline images/equations; "
                f"passing through untouched"
            )
        if mixed_media:
            progress(
                f"      NOTE: {len(mixed_media)} paragraph(s) mix text with "
                f"images/equations; left untranslated (see {log_path.name})"
            )
            logger.warning(
                f"{len(mixed_media)} paragraph(s) have Korean text alongside "
                f"inline media; not translated to keep media intact"
            )
            for r in mixed_media:
                snippet = re.sub(r"\s+", " ", r["text"])[:120]
                logger.warning(f"  mixed-media paragraph idx={r['index']}: '{snippet}…'")

        # ----- 1) claims (single LLM call, all claims at once) -----
        claims_block = collect_block(records, "claim")
        claims_en: dict[str, str] = {}
        glossary: dict[str, str] = {}
        if claims_block:
            n_paras = sum(1 for r in records if r["role"] == "claim")
            progress(f"[1/3] Translating claims ({n_paras} paragraphs, single LLM call)…")
            try:
                claims_en, glossary = translate_claims(
                    client, claims_block, records, progress=progress, logger=logger
                )
                progress(f"      got {len(claims_en)} claim(s); glossary={len(glossary)} entries")
                logger.info(f"claims translated: {len(claims_en)}; glossary entries: {len(glossary)}")
            except Exception as e:
                progress(f"      ERROR: claims translation failed — {e!r}")
                logger.exception("claims translation failed entirely")
        else:
            progress("[1/3] No claims section found — skipping.")
            logger.info("no claims section")

        # ----- 2) abstract -----
        abstract_block = collect_block(records, "abstract")
        abstract_en = ""
        if abstract_block:
            progress("[2/3] Translating abstract…")
            try:
                abstract_en = translate_abstract(client, abstract_block, glossary)
            except Exception as e:
                progress(f"      ERROR: abstract translation failed — {e!r}")
                logger.exception("abstract translation failed")
        else:
            progress("[2/3] No abstract section found — skipping.")
            logger.info("no abstract section")

        # ----- 3) description, paragraph-by-paragraph with rolling context -----
        desc_records = [r for r in records if r["role"] == "description" and r["text"].strip()]
        progress(
            f"[3/3] Translating description ({len(desc_records)} paragraphs, "
            f"context window={DESCRIPTION_CONTEXT_WINDOW})…"
        )
        total = len(desc_records)
        failures = 0
        # Only successful (korean, english) pairs feed back as context — a failed
        # paragraph stays in the source as Korean and is omitted from context so
        # it can't pollute the next call's terminology.
        context: deque = deque(maxlen=DESCRIPTION_CONTEXT_WINDOW)
        for n, r in enumerate(desc_records, 1):
            try:
                translation = translate_paragraph(
                    client, r["text"], glossary, context=context
                )
                r["translation"] = translation
                context.append((r["text"], translation))
            except Exception as e:
                failures += 1
                snippet = r["text"][:160].replace("\n", " ")
                logger.error(
                    f"description paragraph idx={r['index']} failed: {e!r}; korean='{snippet}…'"
                )
            if n % 10 == 0 or n == total:
                progress(f"      {n}/{total}")
        if failures:
            progress(f"      WARNING: {failures} paragraph(s) failed; Korean left in place (see {log_path.name})")
            logger.warning(f"{failures} description paragraph(s) failed to translate")

        # ----- write back -----
        apply_section_headers(doc, records)
        apply_claims(doc, records, claims_en, progress=progress)
        apply_abstract(doc, records, abstract_en)
        for r in desc_records:
            translation = r.get("translation")
            if translation:
                set_paragraph_text(doc.paragraphs[r["index"]], translation)
            # else: paragraph failed — Korean stays so reviewer sees it

        apply_output_font(doc)

        doc.save(out_path)
        progress(f"wrote {out_path}")
        logger.info(f"wrote {out_path}")
    except Exception:
        logger.exception("translate_file failed")
        raise
    finally:
        _teardown_logger(logger)
