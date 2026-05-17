"""Translate the same source file with multiple models and report
timing + Korean-leak ratio side-by-side.

Usage:
    uv run compare_models.py data/sample_linebreak_ids.docx \
        --models qwen3.5:122b gemma4:31b gpt-oss:120b

Each model writes to output/<stem>_<model>.docx. A summary table is
printed at the end. ``--no-review`` is forced for speed.

If a model isn't loaded in Ollama, that run is skipped (the next is tried).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from docx import Document

from translate.agent.docx_utils import (
    extract_all_text,
    has_drawing,
    has_math,
    iter_all_paragraphs,
)
from translate.client import ClientConfig
from translate.translator import PatentTranslator


_HANGUL_RE = re.compile(r"[가-힯]")


def _korean_ratio(path: Path) -> tuple[int, int, int, float]:
    """Walk the docx, return (paragraphs, paragraphs_with_korean, korean_chars, total_chars)."""
    try:
        doc = Document(str(path))
    except Exception:
        return 0, 0, 0, 0.0
    n_total = 0
    n_ko_paras = 0
    ko_chars = 0
    total_chars = 0
    for para in iter_all_paragraphs(doc):
        text = extract_all_text(para)
        if not text.strip():
            continue
        n_total += 1
        total_chars += len(text)
        n_ko = sum(1 for c in text if _HANGUL_RE.fullmatch(c))
        if n_ko:
            n_ko_paras += 1
            ko_chars += n_ko
    return n_total, n_ko_paras, ko_chars, total_chars


def _safe_filename(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", model)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Korean .docx to translate")
    parser.add_argument(
        "--models", nargs="+", required=True,
        help="Model names to compare (e.g. qwen3.5:122b gemma4:31b)",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output"),
        help="Where to write per-model outputs (default: output/)",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Input not found: {args.input}")
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)

    base_config = ClientConfig.from_env()
    results: list[dict] = []

    for model in args.models:
        out_path = args.output_dir / f"{args.input.stem}_{_safe_filename(model)}.docx"
        log_path = out_path.with_suffix(".log")

        config = ClientConfig(
            model=model,
            base_url=base_config.base_url,
            api_key=base_config.api_key,
            temperature=base_config.temperature,
            max_tokens=base_config.max_tokens,
        )
        translator = PatentTranslator(config)
        print("=" * 60)
        print(f"  Translating with {model}")
        print(f"  output: {out_path}")
        print("=" * 60)
        t0 = time.monotonic()
        ok = True
        err_msg = ""
        try:
            translator.translate_document(
                args.input, out_path,
                review=False,
                verbose=True,
                log_path=log_path,
            )
        except Exception as exc:
            ok = False
            err_msg = f"{type(exc).__name__}: {exc}"
        dt = time.monotonic() - t0

        n_total, n_ko_paras, ko_chars, total_chars = (
            _korean_ratio(out_path) if out_path.exists() else (0, 0, 0, 0)
        )
        results.append({
            "model": model,
            "ok": ok,
            "err": err_msg,
            "wall_seconds": dt,
            "out_path": out_path,
            "n_total": n_total,
            "n_ko_paras": n_ko_paras,
            "ko_chars": ko_chars,
            "total_chars": total_chars,
        })
        print()

    # Summary table
    print("=" * 78)
    print("  COMPARISON SUMMARY")
    print("=" * 78)
    print(f"  {'model':<22}{'status':<10}{'wall':>8}  {'paras':>6} {'KO_paras':>9} {'KO_%':>7}")
    for r in results:
        status = "ok" if r["ok"] else "FAIL"
        ko_pct = (r["ko_chars"] / r["total_chars"] * 100) if r["total_chars"] else 0.0
        print(
            f"  {r['model']:<22}{status:<10}{r['wall_seconds']:>6.1f}s  "
            f"{r['n_total']:>6} {r['n_ko_paras']:>9} {ko_pct:>6.2f}%"
        )
        if not r["ok"]:
            print(f"      error: {r['err']}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
