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

# Copy and fill in credentials
cp .env.example .env
```

## Project Structure

```
main.py           — Single-file CLI
batch.py          — Batch translation with multiprocessing + S3 support
download.py       — Download .docx files from S3 to a local directory
preprocess.py     — Strip paragraph numbering ([0016]) from raw docx files
translate/
  client.py       — OpenAI-compatible LLM client (works with Ollama, OpenAI, etc.)
  agent/prompts.py — Section-specific prompts + agent prompt builders
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

## Downloading from S3

Download `.docx` files from S3 to a local directory without running translation:

```bash
# Uses S3_* values from .env
uv run python download.py

# Override any value on the command line
uv run python download.py --bucket my-bucket --prefix patents/2024/ --dir data/batch3
```

Configure S3 credentials in `.env`:

```ini
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
S3_ENDPOINT_URL=          # leave empty for AWS; set for MinIO / R2 / etc.
S3_BUCKET=my-patent-bucket
S3_PREFIX=patents/korean/
S3_DOWNLOAD_DIR=data/s3
```

Already-downloaded files are skipped on re-runs.

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
| `--context-window` | `3` | Preceding paragraphs passed as rolling context |
| `--lookahead` | `2` | Upcoming paragraphs included as read-only context |
| `--font` | `Times New Roman` | Output font |
| `--delay` | `0.5` | Seconds between API calls |
| `--no-review` | — | Skip the post-translation review pass |
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
LOCAL_DIRS = ["data"]   # scanned recursively for .docx files
```

### S3 files

```python
USE_S3 = True   # reads S3_* credentials from .env
```

Files are downloaded to `S3_DOWNLOAD_DIR` before translation. Already-downloaded files are skipped on re-runs.

### Batch configuration

```python
WORKERS = 2          # files processed in parallel
CONTEXT_WINDOW = 3   # rolling backward context (paragraph pairs)
LOOKAHEAD_WINDOW = 2 # read-only forward context (raw paragraphs)
REVIEW = True        # post-translation review pass per section
FONT = "Times New Roman"
DELAY = 0.5          # seconds between API calls within one file
```

With a local Ollama model, `WORKERS > 1` does not reduce wall-clock time for a single model. Increase it when using an API provider that supports concurrent requests.

## Translation Pipeline

Each document goes through two passes:

### 1. Translation pass

Paragraphs are translated one at a time using section-specific prompts:

- **Section routing** — body, abstract, and claims each use a dedicated prompt tuned for USPTO style.
- **Rolling context** — the last `--context-window` translated paragraph pairs are injected as conversation history to maintain terminology consistency.
- **Lookahead context** — the next `--lookahead` raw paragraphs are appended to each prompt as read-only context, helping with multi-part constructs like enumerated lists and multi-clause claims.
- **Claim formatting** — `【청구항 N】` markers are replaced with `N.`; the claim body is translated without a number prefix.
- **Abstract word count** — inserted as `(N)` immediately after the abstract.
- **Images and equations** — preserved from the source document; only text runs are translated.
- **Line breaks** — sentences break at `.` and `;` boundaries using `<w:br/>` elements so breaks render correctly in Word.
- **Unicode normalisation** — non-breaking hyphens, curly quotes, and special spaces are converted to ASCII equivalents to avoid rendering issues in Times New Roman.

### 2. Review pass (per section)

After each section is fully translated, a two-step review runs:

1. **Decision node** — sends the full set of Korean/English paragraph pairs to the LLM. Returns `{ "needs_revision": bool, "issues": [...] }`. If no issues are found, the section is kept as-is (no extra API call).
2. **Revision** — if issues were found, a second call receives the pairs plus the issue list and returns only the paragraphs that need changes. Revisions are applied back to the document in place.

The review checks for:
- Terminology drift (same Korean term translated differently across paragraphs)
- Translation omissions or hallucinations
- Claim structure (`A ... comprising:` / `The ... of claim N, wherein`)
- Figure reference format (`FIG. N`)

Disable with `--no-review` (CLI) or `REVIEW = False` (batch).
