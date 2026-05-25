#!/usr/bin/env bash
set -euo pipefail

# Enqueue every .docx file under a directory into taskspooler.
#
# Usage:
#   ./run_data_ts.sh                      # enqueue data/**/*.docx
#   ./run_data_ts.sh data/batch2          # enqueue another directory
#   ./run_data_ts.sh data --no-review     # pass extra args to main.py
#
# Environment:
#   TS_SLOTS=1   taskspooler concurrency. Default is 1 so jobs run one by one.

DATA_DIR="data"
if [[ $# -gt 0 && "$1" != -* ]]; then
  DATA_DIR="$1"
  shift
fi

TS_SLOTS="${TS_SLOTS:-1}"

if ! command -v ts >/dev/null 2>&1; then
  echo "ERROR: taskspooler command 'ts' was not found in PATH." >&2
  echo "Install taskspooler or run translations directly with: uv run python main.py <file.docx>" >&2
  exit 1
fi

if [[ ! -d "$DATA_DIR" ]]; then
  echo "ERROR: data directory not found: $DATA_DIR" >&2
  exit 1
fi

mkdir -p output

# Force one-at-a-time execution by default. Override with TS_SLOTS=N if your
# provider/model can safely handle concurrent translations.
ts -S "$TS_SLOTS" >/dev/null

# Read the file list first, sorted, then enqueue. This gives deterministic job
# order and avoids surprises if files are added while the script is running.
mapfile -d '' files < <(find "$DATA_DIR" -type f -name '*.docx' ! -name '~$*' -print0 | sort -z)

if [[ "${#files[@]}" -eq 0 ]]; then
  echo "No .docx files found under $DATA_DIR"
  exit 0
fi

echo "Taskspooler slots: $TS_SLOTS"
echo "Found ${#files[@]} .docx file(s) under $DATA_DIR"

count=0
for file in "${files[@]}"; do
  count=$((count + 1))
  printf 'Enqueue [%d/%d] %s\n' "$count" "${#files[@]}" "$file"
  # Equivalent to: ts uv run python main.py "$file" [extra main.py args]
  ts uv run python main.py "$file" "$@"
done

echo "Queued $count file(s)."
echo "Useful commands:"
echo "  ts          # show queue"
echo "  ts -c ID    # show output for job ID"
echo "  ts -S 1     # keep running one job at a time"
