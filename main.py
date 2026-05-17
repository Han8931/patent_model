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
        default=0.0,
        help="Seconds to wait between API calls (default: 0.0; set >0 only if your provider rate-limits)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=30,
        help="Max paragraphs per section batch sent to the LLM (default: 30)",
    )
    parser.add_argument(
        "--font", default="Times New Roman", help="Output font name (default: Times New Roman)"
    )
    parser.add_argument("--no-review", action="store_true", help="Skip the post-translation review pass")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="Translation log path (default: same as output with .log suffix)",
    )
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


def _print_config(config: ClientConfig, args: argparse.Namespace, output: Path) -> None:
    """Print a brief config summary so a user can confirm the run setup."""
    try:
        from translate.agent.nodes.chunk_body import _CHUNK_MAX_CHARS
        chunk_chars = _CHUNK_MAX_CHARS
    except Exception:
        chunk_chars = "?"
    print("=" * 60)
    print(f"  input            : {args.input}")
    print(f"  output           : {output}")
    print(f"  model            : {config.model}")
    print(f"  base_url         : {config.base_url}")
    print(f"  temperature      : {config.temperature}")
    print(f"  max_tokens       : {config.max_tokens}")
    print(f"  body chunk chars : {chunk_chars}")
    print(f"  batch_size       : {args.batch_size}")
    print(f"  font             : {args.font}")
    print(f"  review           : {not args.no_review}")
    print("=" * 60)


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

    if not args.quiet:
        _print_config(config, args, output)

    translator = PatentTranslator(config, batch_size=args.batch_size)
    try:
        translator.translate_document(
            args.input,
            output,
            font=args.font,
            delay=args.delay,
            verbose=not args.quiet,
            review=not args.no_review,
            log_path=args.log,
            progress_callback=(lambda _: None) if args.quiet else None,
        )
    except Exception:
        # On any failure, remove a partial output so the user can't mistake a
        # half-written or zero-translated file for a successful run.
        try:
            if output.exists():
                output.unlink()
        except OSError:
            pass
        raise


if __name__ == "__main__":
    main()
