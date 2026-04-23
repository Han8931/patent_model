"""Patent translation CLI entry point."""

import argparse
from pathlib import Path

from translate.client import ClientConfig
from translate.translator import PatentTranslator


def parse_args() -> argparse.Namespace:
    # Load .env so defaults below reflect the current environment config
    from dotenv import load_dotenv
    import os
    load_dotenv()

    env = ClientConfig.from_env()  # used only to derive defaults for help text

    parser = argparse.ArgumentParser(
        description="Translate a Korean patent application docx to English."
    )
    parser.add_argument("input", type=Path, help="Cleaned Korean patent .docx file")
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="Output path: a .docx file or a directory (default: output/)",
    )
    parser.add_argument(
        "--model", default=env.model,
        help=f"Model name (default: {env.model})",
    )
    parser.add_argument(
        "--base-url", default=env.base_url,
        help=f"OpenAI-compatible API base URL (default: {env.base_url})",
    )
    parser.add_argument(
        "--api-key", default=env.api_key,
        help="API key (overrides LLM_API_KEY in .env)",
    )
    parser.add_argument(
        "--temperature", type=float, default=env.temperature,
        help=f"Sampling temperature (default: {env.temperature})",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=env.max_tokens,
        help=f"Max tokens per response (default: {env.max_tokens})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait between API calls (default: 0.5)",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=3,
        help="Number of preceding paragraphs to pass as context (default: 3, 0 to disable)",
    )
    parser.add_argument(
        "--font", default="Times New Roman", help="Output font name (default: Times New Roman)"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    return parser.parse_args()


def resolve_output(input_path: Path, output_arg: Path | None) -> Path:
    """Resolve the output file path.

    - No argument → output/<input_stem>_en.docx
    - Directory    → <dir>/<input_stem>_en.docx
    - File path    → used as-is
    """
    if output_arg is None or output_arg.is_dir() or not output_arg.suffix:
        directory = output_arg if output_arg is not None else Path("output")
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{input_path.stem}_en.docx"
    return output_arg


def main() -> None:
    args = parse_args()

    config = ClientConfig(
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )

    output = resolve_output(args.input, args.output)

    translator = PatentTranslator(config, context_window=args.context_window)
    translator.translate_document(
        args.input,
        output,
        font=args.font,
        delay=args.delay,
        verbose=not args.quiet,
    )


if __name__ == "__main__":
    main()
