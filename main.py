"""CLI for translating a single Korean patent .docx into English (USPTO style).

Usage:
    uv run python main.py path/to/korean.docx [output.docx]
    uv run python main.py path/to/korean.docx --suffix _v1 --model qwen3.5

The actual pipeline lives in ``translate/translator.py``; this file is just
argparse + a startup banner + an elapsed-time line.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from translate import resolve_output_path, translate_file
from translate.client import ClientConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate a Korean patent .docx to English.")
    parser.add_argument("path", type=Path, help="Input .docx file")
    parser.add_argument("output", type=Path, nargs="?", default=None,
                        help="Output .docx (default: output/<stem>_en.docx)")
    parser.add_argument("--suffix", default="",
                        help="Suffix inserted before .docx (e.g. --suffix _v1 → ..._en_v1.docx)")
    parser.add_argument("--model", default=None,
                        help="Override LLM_MODEL from .env (e.g. --model qwen3.5)")
    args = parser.parse_args()

    if not args.path.is_file():
        sys.exit(f"input not found: {args.path}")

    out_path = resolve_output_path(args.path, args.output, args.suffix)

    config = ClientConfig.from_env()
    model = args.model or config.model
    print(f"input:  {args.path}")
    print(f"output: {out_path}")
    print(f"model:  {model} @ {config.base_url}")
    print()

    t0 = time.monotonic()
    translate_file(args.path, out_path, model=args.model)
    print(f"\nelapsed: {time.monotonic() - t0:.1f}s")


if __name__ == "__main__":
    main()
