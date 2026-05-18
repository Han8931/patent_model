"""Batch-translate every Korean patent .docx under a directory.

Usage:
    uv run python batch.py                     # data/, 1 worker, no suffix
    uv run python batch.py --worker 5
    uv run python batch.py --worker 5 --suffix _v1
    uv run python batch.py --data-dir data/batch3 --worker 3

Each file is processed end-to-end by ``main.translate_file``. With multiple
workers the per-file progress prints are prefixed with the file name so the
interleaved output stays readable.
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from main import resolve_output_path, translate_file
from translate.client import ClientConfig


def _discover(data_dir: Path) -> list[Path]:
    """All .docx files under *data_dir* (recursive), skipping Word lock files."""
    return sorted(f for f in data_dir.rglob("*.docx") if not f.name.startswith("~$"))


def _process(
    idx: int, total: int, in_path: Path, suffix: str, model: str | None
) -> tuple[Path, float, str | None]:
    """Translate one file. Returns (path, elapsed_seconds, error_or_None)."""
    prefix = f"[{idx}/{total}] {in_path.name}"
    log = lambda msg: print(f"{prefix} {msg}", flush=True)

    out_path = resolve_output_path(in_path, None, suffix)
    t0 = time.monotonic()
    try:
        translate_file(in_path, out_path, progress=log, model=model)
        return in_path, time.monotonic() - t0, None
    except Exception as e:
        return in_path, time.monotonic() - t0, f"{type(e).__name__}: {e}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-translate Korean patent .docx files.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"),
                        help="Directory to scan for .docx files (default: data)")
    parser.add_argument("--worker", "--workers", dest="workers", type=int, default=1,
                        help="Parallel worker count (default: 1)")
    parser.add_argument("--suffix", default="",
                        help="Suffix appended to each output stem (e.g. --suffix _v1)")
    parser.add_argument("--model", default=None,
                        help="Override LLM_MODEL from .env (e.g. --model qwen3.5)")
    args = parser.parse_args()

    if not args.data_dir.is_dir():
        raise SystemExit(f"not a directory: {args.data_dir}")

    files = _discover(args.data_dir)
    if not files:
        print(f"no .docx files found under {args.data_dir}")
        return

    total = len(files)
    workers = max(1, args.workers)

    config = ClientConfig.from_env()
    model = args.model or config.model
    print(f"data:    {args.data_dir}  ({total} file(s))")
    print(f"workers: {workers}")
    print(f"suffix:  {args.suffix!r}")
    print(f"model:   {model} @ {config.base_url}")
    print()

    failures: list[tuple[Path, str]] = []
    t_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [
            ex.submit(_process, i + 1, total, f, args.suffix, args.model)
            for i, f in enumerate(files)
        ]
        for fut in as_completed(futures):
            in_path, dur, err = fut.result()
            if err:
                print(f"FAIL  {in_path.name}  ({dur:.1f}s)  {err}", flush=True)
                failures.append((in_path, err))
            else:
                print(f"OK    {in_path.name}  ({dur:.1f}s)", flush=True)

    elapsed = time.monotonic() - t_start
    succeeded = total - len(failures)
    print(f"\ndone — {succeeded}/{total} succeeded in {elapsed:.1f}s")
    if failures:
        for path, err in failures:
            print(f"  FAIL {path}: {err}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
