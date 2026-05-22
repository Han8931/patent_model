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
inspect.py        — Inspect DOCX parsing/chunking before translation
preprocess.py     — Strip paragraph numbering ([0016]) from raw docx files
translate/
  client.py       — OpenAI-compatible LLM client (works with Ollama, OpenAI, etc.)
  agent/prompts.py — Consolidated prompts for translation, glossary, and review
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

## Inspecting DOCX Files

Before translating a new source set, inspect parsing and chunking readiness:

```bash
# Inspect one document
uv run python inspect.py data/document.docx

# Inspect every .docx under a directory
uv run python inspect.py data/
```

The inspector reports top-level vs recursive paragraph counts, detected sections,
body/abstract/claim chunks, table-wrapped text, mixed text/image paragraphs, and
math-bearing paragraphs. This is useful because Word files often store text in
tables, fields, fragmented runs, and mixed image-equation paragraphs.

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
| `--font` | `Times New Roman` | Output font |
| `--font-size` | `12` | Output font size in points |
| `--delay` | `0.0` | Seconds between API calls |
| `--no-review` | — | Skip the post-translation review pass |
| `--quiet` | — | Suppress progress output |
| `--log` | `<output>.log` | Translation log path |

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
REVIEW = True        # post-translation review pass per section
FONT = "Times New Roman"
DELAY = 0.0          # seconds between API calls within one file
```

With a local Ollama model, `WORKERS > 1` does not reduce wall-clock time for a single model. Increase it when using an API provider that supports concurrent requests.

## Taskspooler Queue

To enqueue every `.docx` file under `data/` with taskspooler:

```bash
./run_data_ts.sh
```

The script submits one job per file, equivalent to:

```bash
ts uv run python main.py data/filename.docx
```

You can pass a different input directory as the first argument:

```bash
./run_data_ts.sh data/batch2
```

Any additional arguments are passed through to `main.py`:

```bash
./run_data_ts.sh data --model gpt-oss:120b-q8_0 --delay 0.5
./run_data_ts.sh data --no-review
./run_data_ts.sh --no-review
```

Useful taskspooler commands:

```bash
ts          # show queued/running/completed jobs
ts -c 0     # show output for job 0
ts -S 1     # run one queued job at a time
```

## Translation Flow

The current pipeline is claim-first so that claim terminology drives the rest of
the specification:

1. **Load and classify** — recursively reads Word paragraphs, including
   table-wrapped text, fields, hyperlinks/REF display text, line breaks, tabs,
   math, and image-based equations.
2. **Static normalization** — maps Korean section headers to English patent
   headings and preserves claim headers until the claim writer replaces them.
3. **Claims first** — chunks claims, plans independent-claim preambles, translates
   independent claims before dependent claims, and builds a glossary from claim
   terminology.
4. **Claim review/fix** — reviews claim consistency, USPTO preambles,
   dependencies, antecedent basis, reference numerals, figure style, and logical
   contradictions. If issues are found, the next log line is `Revising CLAIMS…`.
5. **Description/body** — translates using the claim-derived glossary, then
   updates the glossary with new description terms where appropriate.
6. **Body review/fix** — reviews terminology, omissions, artifacts, Korean
   leftovers, equations, reference numerals, and USPTO style.
7. **Abstract** — translates after the claim/body terminology is established and
   inserts the abstract word-count footer.
8. **Write DOCX** — writes translations back while preserving equations/images,
   normalizes `FIG.` references, applies Times New Roman/12 pt by default, checks
   math integrity, and refuses to save if too much Hangul remains.

Progress is printed every 10 items and at completion for long-running stages:
`PREAMBLE 10/...`, `BODY 10/...`, `CLAIM 10/...`, and `WRITE 10/...`.

## Output Format Rules

Translation outputs are plain English text. The translator does not require JSON
for translated claims, body, or abstract text. JSON is used only for internal
structured helper calls such as preamble planning, review decisions, revision
lists, and glossary extraction.

Key formatting behavior:

- **USPTO style** — claims use `comprising`, `wherein`, `further comprising`,
  `A <noun phrase> comprising:`, and `The <noun phrase> of claim N, wherein`.
- **Dependent claims** — dependency preambles are enforced after translation and
  after review revisions.
- **Antecedent basis** — prompts require `a/an` for first introduction and `the`
  for later references; glossary terms are not treated as antecedent basis.
- **Figure references** — normalized to `FIG. N` or `FIGS. N and M`.
- **Reference numerals/characters** — preserved as written, including forms such
  as `100`, `100a`, `GR(1)`, `T1`, and `S10`.
- **Possessives** — technical component relationships prefer `of` constructions,
  such as `a surface of the substrate`, over apostrophe possessives.
- **Line breaks** — actual Word line breaks are preserved; paragraph IDs such as
  `[0001]` do not by themselves force a new paragraph or line break.
- **Equations and images** — inline image-equations and OMML equations are kept
  at their source positions when they appear inside a paragraph.
- **Fonts** — generated output is normalized to Times New Roman, 12 pt by
  default, including ASCII, East Asian, and complex-script font slots.

## Review and Retry Logic

Each translation unit can be retried when the model returns unusable output.
The validator checks for:

- no response or empty output
- placeholder text
- refusal/no-input messages
- markdown tables or fenced code
- JSON/schema artifacts in translation text
- remaining Korean/Hangul text

The translator retries up to three more times. On the final retry, it switches
to a simpler fallback prompt, for example: `Translate this Korean patent
specification text into USPTO style.`

The review pass is also defensive:

1. **Decision** — reports issues such as terminology drift or claim logic errors.
2. **Revision** — runs only if issues were detected.
3. **Validation** — applies only safe revisions. Revisions containing Korean,
   markdown/JSON artifacts, or invalid claim preambles are skipped.

Disable review with `--no-review` (CLI) or `REVIEW = False` (batch).
