"""Batch patent translation — process multiple files in parallel.

File source: either a local list or an S3 bucket (configured via .env).
Edit the CONFIGURATION section below before running.
"""

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

CONFIG = ClientConfig.from_env()   # reads LLM_* variables from .env

WORKERS = 2          # parallel files; keep ≤ Ollama concurrency limit
BATCH_SIZE = 10      # max paragraphs per section batch sent to the LLM
REVIEW = True        # run post-translation review pass per section
FONT = "Times New Roman"
DELAY = 0.5          # seconds between API calls within one file


# ---------------------------------------------------------------------------
# Worker (top-level function — required for multiprocessing pickle)
# ---------------------------------------------------------------------------

def _translate_file(job: dict) -> dict:
    """Translate one file. Returns a result dict with status and paths."""
    input_path = Path(job["input"])
    output_path = Path(job["output"])
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
            progress_callback=_progress,
        )
        _progress(f"Done → {output_path}")
        return {"input": str(input_path), "output": str(output_path), "ok": True}

    except Exception:
        print(f"[{name}] FAILED:\n{traceback.format_exc()}")
        return {"input": str(input_path), "output": str(output_path), "ok": False}


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


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    input_files = _resolve_files()
    if not input_files:
        print("No input files found. Exiting.")
        return

    jobs = [
        {
            "input":          str(inp),
            "output":         str(OUTPUT_DIR / f"{inp.stem}_en.docx"),
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
            print(f"  {r['input']}")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
