# Patent Model

Korean → English patent application translator. Reads cleaned Korean `.docx` files and produces English `.docx` output following USPTO patent application style.

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv)
- [Ollama](https://ollama.com) (or any OpenAI-compatible API)

## Setup

```bash
# Install dependencies
uv sync

# Pull the model (Ollama)
ollama pull gpt-oss:120b          # MXFP4 quantized (~60 GB)
ollama pull gpt-oss:120b-q8_0     # Q8 (~120 GB, recommended for Mac Studio 256 GB)
ollama pull gpt-oss:120b-fp16     # Full precision (~240 GB)

# Copy and fill in credentials (only needed for S3 batch mode)
cp .env.example .env
```

## Project Structure

```
batch.py          — Batch translation with multiprocessing + S3 support
main.py           — Single-file CLI
preprocess.py     — Strip paragraph numbering ([0016]) from raw docx files
translate/
  client.py       — OpenAI-compatible LLM client (works with Ollama, OpenAI, etc.)
  prompt.py       — Prompt registry with section-specific prompts (body / abstract / claims)
  translator.py   — Core translation engine
  s3.py           — S3 file listing and download helpers
data/             — Input documents (git-ignored)
output/           — Translated documents (git-ignored)
```

## Preprocessing

Strip the `[NNNN]` paragraph numbering from raw Korean patent docx files before translating:

```bash
uv run python preprocess.py
```

Outputs `data/published1_kr_clean.docx` and `data/published1_en_clean.docx`.

## Single File

```bash
# Default: Ollama on localhost, output to output/
uv run python main.py data/published1_kr_clean.docx

# Explicit output path
uv run python main.py data/published1_kr_clean.docx output/result.docx

# OpenAI
uv run python main.py data/published1_kr_clean.docx \
  --base-url https://api.openai.com/v1 \
  --api-key sk-... \
  --model gpt-4o
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--model` | `gpt-oss:120b` | Model name |
| `--base-url` | `http://localhost:11434/v1` | API base URL |
| `--api-key` | `ollama` | API key |
| `--temperature` | `0.2` | Sampling temperature |
| `--max-tokens` | `4096` | Max tokens per response |
| `--context-window` | `3` | Preceding paragraphs passed as context |
| `--font` | `Times New Roman` | Output font |
| `--delay` | `0.5` | Seconds between API calls |
| `--quiet` | — | Suppress progress output |

## Batch Mode

Edit the configuration block at the top of `batch.py`, then run:

```bash
uv run python batch.py
```

### Local files

```python
# batch.py
USE_S3 = False
LOCAL_FILES = [
    "data/published1_kr_clean.docx",
    "data/published2_kr_clean.docx",
]
```

### S3 files

Set `USE_S3 = True` in `batch.py` and configure `.env`:

```ini
# .env
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
S3_BUCKET=my-patent-bucket
S3_PREFIX=patents/korean/
S3_DOWNLOAD_DIR=data/s3
```

Files are downloaded to `S3_DOWNLOAD_DIR` before translation. Already-downloaded files are skipped on re-runs.

### Tuning parallelism

```python
WORKERS = 2   # files processed in parallel
```

With a local Ollama model, requests are queued server-side so `WORKERS > 1` does not reduce wall-clock time for a single model. Increase `WORKERS` when using an API provider that supports concurrent requests.

## Translation Behaviour

- **Section routing** — body, abstract, and claims sections each use a dedicated prompt tuned for USPTO style.
- **Rolling context** — the last `--context-window` paragraph pairs are injected as conversation history to maintain terminology consistency.
- **Claim formatting** — `【청구항 N】` markers are detected and replaced with canonical `N.` prefixes; dependent claim phrasing (`of claim N`) is enforced by the claims prompt.
- **Abstract word count** — inserted as `(N)` immediately after the abstract text.
- **Images and equations** — preserved from the source document; only text runs are translated.
- **Line breaks** — sentences break at `.` and `;` boundaries; `<w:br/>` elements are used so breaks render correctly in Word.
- **Unicode normalisation** — special hyphens, curly quotes, and non-breaking spaces are converted to ASCII equivalents to avoid rendering issues in Times New Roman.
