"""retry_failed.py — Re-translate just the Korean paragraphs from a partial output.

When ``main.py`` / ``batch.py`` finishes with any paragraph still in Korean,
the strict write-time guard in ``translate/agent/nodes/write.py`` refuses to
save the canonical ``<output>.docx`` and instead saves a sibling
``<output>.partial.docx`` snapshot. This script picks up from there: it walks
the partial, finds every paragraph that still contains Hangul, re-runs **only
those paragraphs** through the body prompt, and — once every paragraph is
English — saves the final ``<output>.docx``.

Using this is much faster than re-running the entire document. Failed chunks
left the original Korean source text in place inside the partial, so the
Korean text itself is the input to retry — no need to re-load the source
docx and rebuild the paragraph-index mapping.

    uv run retry_failed.py output/sample_en.partial.docx
    uv run retry_failed.py output/sample_en.partial.docx --output output/sample_en.docx
    uv run retry_failed.py output/sample_en.partial.docx --model qwen3.5:122b
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from docx import Document

from translate.agent.docx_utils import (
    iter_all_paragraphs,
    postprocess,
    replace_text,
)
from translate.agent.prompts import (
    build_body_messages,
    build_body_retry_messages,
    parse_translation_dual_shape,
)
from translate.client import ClientConfig, LLMClient, is_reasoning_model


_HANGUL_RE = re.compile(r"[가-힯]")


def _contains_hangul(text: str | None) -> bool:
    return bool(text and _HANGUL_RE.search(text))


def _find_hangul_paragraphs(doc) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for idx, p in enumerate(iter_all_paragraphs(doc)):
        text = p.text or ""
        if _contains_hangul(text):
            snippet = text[:200].replace("\n", " ⏎ ")
            out.append((idx, snippet))
    return out


def _retranslate(
    client: LLMClient,
    korean_text: str,
    glossary: dict[str, str],
) -> str:
    """Body-style retranslation with one corrective retry.

    Returns the cleaned English text (post-processed) or "" on failure /
    if the response still contained Hangul after the retry.
    """
    messages = build_body_messages(korean_text, glossary)
    last_problem = "Empty or still contained Korean."
    for attempt in range(2):
        try:
            raw = client.complete(messages)
            text, new_terms = parse_translation_dual_shape(raw)
        except Exception as exc:
            last_problem = f"{type(exc).__name__}: {exc}"
            text, new_terms = "", {}
        if text and not _contains_hangul(text):
            for ko, en in new_terms.items():
                glossary.setdefault(ko, en)
            return postprocess(text)
        if not text:
            last_problem = "Empty response."
        elif _contains_hangul(text):
            last_problem = "Response still contained Korean."
        if attempt == 0:
            messages = build_body_retry_messages(
                previous_messages=messages,
                problem=last_problem,
                chunk_text=korean_text,
            )
    return ""


def _parse_args() -> argparse.Namespace:
    env = ClientConfig.from_env()
    parser = argparse.ArgumentParser(
        description="Re-translate just the Korean paragraphs from a "
                    "partial output and save the final docx."
    )
    parser.add_argument(
        "partial",
        type=Path,
        help="Path to <output>.partial.docx saved by a failed run.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Final output path (default: drop '.partial' from the input "
             "filename, e.g. sample_en.partial.docx → sample_en.docx).",
    )
    parser.add_argument(
        "--model", default=env.model,
        help=f"Model name (default: {env.model}).",
    )
    parser.add_argument(
        "--base-url", default=env.base_url,
        help=f"API base URL (default: {env.base_url}).",
    )
    parser.add_argument(
        "--api-key", default=env.api_key,
        help="API key.",
    )
    parser.add_argument(
        "--temperature", type=float, default=env.temperature,
        help=f"Sampling temperature (default: {env.temperature}).",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=env.max_tokens,
        help=f"Max tokens per response (default: {env.max_tokens}).",
    )
    parser.add_argument(
        "--font", default="Times New Roman",
        help="Output font name (default: Times New Roman).",
    )
    parser.add_argument(
        "--keep-partial", action="store_true",
        help="Keep the .partial.docx file after a successful retry (default: delete).",
    )
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="Seconds to wait between API calls (default: 0.0).",
    )
    return parser.parse_args()


def _default_output_for(partial: Path) -> Path:
    """sample_en.partial.docx → sample_en.docx (in the same directory)."""
    name = partial.name
    if name.endswith(".partial.docx"):
        return partial.with_name(name[: -len(".partial.docx")] + ".docx")
    # Fallback: append _retried.docx so we never overwrite the partial.
    return partial.with_name(partial.stem + "_retried.docx")


def main() -> None:
    args = _parse_args()

    if not args.partial.exists():
        sys.exit(f"Partial file not found: {args.partial}")

    output_path = args.output or _default_output_for(args.partial)

    # Reasoning models need a bigger budget — auto-bump exactly like main.py.
    env_defaults = ClientConfig.from_env()
    cli_overrode_max = args.max_tokens != env_defaults.max_tokens
    if (
        not cli_overrode_max
        and is_reasoning_model(args.model)
        and args.max_tokens < 16_384
    ):
        args.max_tokens = 32_768

    config = ClientConfig(
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    client = LLMClient(config)

    doc = Document(str(args.partial))
    korean_paras = _find_hangul_paragraphs(doc)
    if not korean_paras:
        print(f"No Korean paragraphs found in {args.partial}.")
        print("Saving as the canonical output without changes.")
        doc.save(str(output_path))
        print(f"Saved → {output_path}")
        return

    print("=" * 60)
    print(f"  partial          : {args.partial}")
    print(f"  output           : {output_path}")
    print(f"  korean paragraphs: {len(korean_paras)}")
    print(f"  model            : {config.model}")
    print(f"  base_url         : {config.base_url}")
    print(f"  max_tokens       : {config.max_tokens}")
    print("=" * 60)

    paragraphs = list(iter_all_paragraphs(doc))
    glossary: dict[str, str] = {}
    failed: list[int] = []
    started = time.monotonic()

    for n, (idx, snippet) in enumerate(korean_paras, 1):
        para = paragraphs[idx]
        ko = (para.text or "").strip()
        if not ko:
            continue
        t0 = time.monotonic()
        try:
            en = _retranslate(client, ko, glossary)
        except Exception as exc:
            print(f"  [{n:>3}/{len(korean_paras)}] FAIL  para {idx}: "
                  f"{type(exc).__name__}: {exc}")
            failed.append(idx)
            continue
        dt = time.monotonic() - t0
        if not en:
            print(f"  [{n:>3}/{len(korean_paras)}] empty para {idx}  {dt:5.1f}s  "
                  f"({snippet[:80]})")
            failed.append(idx)
            continue
        try:
            replace_text(para, en, args.font)
        except Exception as exc:
            print(f"  [{n:>3}/{len(korean_paras)}] write fail para {idx}: "
                  f"{type(exc).__name__}: {exc}")
            failed.append(idx)
            continue
        print(f"  [{n:>3}/{len(korean_paras)}] ok    para {idx}  {dt:5.1f}s")
        if args.delay > 0:
            time.sleep(args.delay)

    elapsed = time.monotonic() - started

    remaining = _find_hangul_paragraphs(doc)
    if remaining:
        # Still Korean — refresh the partial so the next retry sees only the
        # paragraphs that are STILL failing (the ones we just fixed are gone).
        doc.save(str(args.partial))
        print()
        print(f"{len(remaining)} paragraph(s) still contain Korean after retry "
              f"({elapsed:.1f}s elapsed).")
        print(f"Re-saved partial → {args.partial}")
        print("Try again with a stronger model or higher --max-tokens.")
        sys.exit(1)

    doc.save(str(output_path))
    print()
    print(f"All paragraphs clean ({elapsed:.1f}s elapsed).")
    print(f"Saved → {output_path}")
    if not args.keep_partial:
        try:
            args.partial.unlink()
            print(f"Removed partial: {args.partial}")
        except OSError as exc:
            print(f"Could not remove partial {args.partial}: {exc}")


if __name__ == "__main__":
    main()
