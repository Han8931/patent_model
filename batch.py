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
# Set to False to use the LOCAL_FILES list below.
USE_S3 = True

# Used when USE_S3 = False
LOCAL_FILES: list[str] = [
    "data/published1_kr_clean.docx",
    # "data/published2_kr_clean.docx",
]

OUTPUT_DIR = Path("output")

CONFIG = ClientConfig(
    model="gpt-oss:120b",
    base_url="http://localhost:11434/v1",
    api_key="ollama",
    temperature=0.2,
    max_tokens=4096,
)

WORKERS = 2          # parallel files; keep ≤ Ollama concurrency limit
CONTEXT_WINDOW = 3
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
        translator = PatentTranslator(config, context_window=job["context_window"])

        print(f"[{name}] Starting…")
        translator.translate_document(
            input_path,
            output_path,
            font=job["font"],
            delay=job["delay"],
            verbose=False,
        )
        print(f"[{name}] Done → {output_path}")
        return {"input": str(input_path), "output": str(output_path), "ok": True}

    except Exception:
        print(f"[{name}] FAILED:\n{traceback.format_exc()}")
        return {"input": str(input_path), "output": str(output_path), "ok": False}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _resolve_files() -> list[Path]:
    """Return the list of local input paths, downloading from S3 if needed."""
    if USE_S3:
        from translate.s3 import fetch_inputs
        return fetch_inputs()          # reads bucket / prefix / download_dir from .env
    return [Path(f) for f in LOCAL_FILES]


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
            "config":         asdict(CONFIG),
            "context_window": CONTEXT_WINDOW,
            "font":           FONT,
            "delay":          DELAY,
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
