"""Batch patent translation — process multiple files in parallel.

File source: either a local list or an S3 bucket (configured via .env).
Edit the CONFIGURATION section below before running, or pass overrides on the CLI:

    uv run batch.py                                # use defaults below
    uv run batch.py --suffix _translated           # output: <stem>_translated.docx
    uv run batch.py --output-dir results           # write into ./results/
    uv run batch.py --output-name foo.docx         # SINGLE-input only: explicit filename

The --output-name flag is only honored when there is exactly one input file.
"""

import argparse
import multiprocessing as mp
import traceback
from dataclasses import asdict
from pathlib import Path

from translate.client import ClientConfig
from translate.translator import PatentTranslator


# ---------------------------------------------------------------------------
# Configuration — edit these before running
# ---------------------------------------------------------------------------

# Set to True to pull files from S3 (reads .env for credentials + bucket).
# Set to False to scan LOCAL_DIRS for .docx files.
USE_S3 = False

# Directories to scan when USE_S3 = False (searched recursively)
LOCAL_DIRS: list[str] = [
    "data",
    # "data/batch2",
]

OUTPUT_DIR = Path("output")
OUTPUT_SUFFIX = "_en"   # appended to <input_stem> when no --output-name is given

CONFIG = ClientConfig.from_env()   # reads LLM_* variables from .env

WORKERS = 2          # parallel files; keep ≤ Ollama concurrency limit
BATCH_SIZE = 10      # max paragraphs per section batch sent to the LLM
REVIEW = True        # run post-translation review pass per section
FONT = "Times New Roman"
DELAY = 0.0          # seconds between API calls within one file (raise only if rate-limited)


# ---------------------------------------------------------------------------
# Worker (top-level function — required for multiprocessing pickle)
# ---------------------------------------------------------------------------

def _translate_file(job: dict) -> dict:
    """Translate one file. Returns a result dict with status and paths."""
    input_path = Path(job["input"])
    output_path = Path(job["output"])
    log_path = output_path.with_suffix(".log")
    name = input_path.name

    try:
        config = ClientConfig(**job["config"])
        translator = PatentTranslator(config, batch_size=job["batch_size"])

        def _progress(msg: str) -> None:
            print(f"[{name}] {msg}", flush=True)

        _progress("Starting…")
        translator.translate_document(
            input_path,
            output_path,
            font=job["font"],
            delay=job["delay"],
            verbose=False,
            review=job["review"],
            log_path=log_path,
            progress_callback=_progress,
        )
        _progress(f"Done → {output_path}")
        return {
            "input": str(input_path),
            "output": str(output_path),
            "log": str(log_path),
            "ok": True,
        }

    except Exception:
        # Cleanup: never leave a partial / pre-copied file on disk that the
        # user could mistake for a successful translation. The graph creates
        # the output only on a clean run, but a stray write that ran before
        # the post-write sanity check could still leave a half-written file.
        try:
            if output_path.exists():
                output_path.unlink()
        except OSError:
            pass
        print(f"[{name}] FAILED (log: {log_path}):\n{traceback.format_exc()}")
        return {
            "input": str(input_path),
            "output": str(output_path),
            "log": str(log_path),
            "ok": False,
        }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _scan_dirs(dirs: list[str]) -> list[Path]:
    """Collect all .docx files found recursively under the given directories."""
    files: list[Path] = []
    for d in dirs:
        files.extend(sorted(Path(d).rglob("*.docx")))
    return files


def _resolve_files() -> list[Path]:
    """Return the list of local input paths, downloading from S3 if needed."""
    if USE_S3:
        from translate.s3 import fetch_inputs
        return fetch_inputs()
    return _scan_dirs(LOCAL_DIRS)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch patent translation."
    )
    parser.add_argument(
        "--output-dir", type=Path, default=OUTPUT_DIR,
        help=f"Directory for translated files (default: {OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--suffix", default=OUTPUT_SUFFIX,
        help=f"Suffix appended to <stem> when --output-name is not used "
             f"(default: '{OUTPUT_SUFFIX}', producing '<stem>{OUTPUT_SUFFIX}.docx').",
    )
    parser.add_argument(
        "--output-name", type=str, default=None,
        help="Explicit output filename (e.g. 'mydoc_en.docx'). Only valid for a "
             "single input file; ignored with a warning if multiple inputs are found.",
    )
    return parser.parse_args()


def _output_path_for(inp: Path, args: argparse.Namespace, single_input: bool) -> Path:
    if single_input and args.output_name:
        name = args.output_name
        if not name.lower().endswith(".docx"):
            name += ".docx"
        return args.output_dir / name
    return args.output_dir / f"{inp.stem}{args.suffix}.docx"


def main() -> None:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    input_files = _resolve_files()
    if not input_files:
        print("No input files found. Exiting.")
        return

    if args.output_name and len(input_files) != 1:
        print(
            f"Warning: --output-name is ignored because {len(input_files)} input "
            f"files were found; using --suffix='{args.suffix}' instead.",
            flush=True,
        )

    single = len(input_files) == 1
    jobs = [
        {
            "input":          str(inp),
            "output":         str(_output_path_for(inp, args, single)),
            "config":      asdict(CONFIG),
            "batch_size":  BATCH_SIZE,
            "review":      REVIEW,
            "font":             FONT,
            "delay":            DELAY,
        }
        for inp in input_files
    ]

    print(f"Batch: {len(jobs)} file(s), {WORKERS} worker(s)\n")

    with mp.Pool(processes=min(WORKERS, len(jobs))) as pool:
        results = pool.map(_translate_file, jobs)

    ok  = [r for r in results if r["ok"]]
    err = [r for r in results if not r["ok"]]
    print(f"\n{'='*50}")
    print(f"Completed: {len(ok)}/{len(results)}")
    if err:
        print("Failed:")
        for r in err:
            print(f"  {r['input']}  (log: {r.get('log')})")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
