#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="data"
if [[ $# -gt 0 && "$1" != -* ]]; then
  DATA_DIR="$1"
  shift
fi

if ! command -v ts >/dev/null 2>&1; then
  echo "ERROR: taskspooler command 'ts' was not found in PATH." >&2
  echo "Install taskspooler or run translations directly with uv run python main.py." >&2
  exit 1
fi

if [[ ! -d "$DATA_DIR" ]]; then
  echo "ERROR: data directory not found: $DATA_DIR" >&2
  exit 1
fi

mkdir -p output

count=0
while IFS= read -r -d '' file; do
  count=$((count + 1))
  printf 'Enqueue [%d] %s\n' "$count" "$file"
  ts uv run python main.py "$file" "$@"
done < <(find "$DATA_DIR" -type f -name '*.docx' ! -name '~$*' -print0)

if [[ "$count" -eq 0 ]]; then
  echo "No .docx files found under $DATA_DIR"
else
  echo "Queued $count file(s). Use 'ts' to view the queue and 'ts -c <jobid>' to view job output."
fi
