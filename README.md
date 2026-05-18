# Patent Model

Korean → English patent application translator. Reads cleaned Korean `.docx` files and produces English `.docx` output following USPTO patent application style.

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv)
- [Ollama](https://ollama.com) (or any OpenAI-compatible API: OpenAI, vLLM, llama.cpp, …)

## Setup

```bash
# Install dependencies
uv sync

# Pull a model (Ollama)
ollama pull gpt-oss:120b          # 60 GB, MXFP4 quantized
ollama pull qwen3.5:122b          # 81 GB, reasoning model
ollama pull gemma4:31b            # 19 GB, non-reasoning, fast
ollama pull qwen2.5:32b           # 19 GB, non-reasoning, very fast

# Copy and fill in credentials
cp .env.example .env
```

## Project Structure

```
main.py             — Single-file CLI
batch.py            — Multi-file translation with multiprocessing + S3 support
download.py         — Download .docx files from S3
preprocess.py       — Strip paragraph numbering ([0016]) from raw docx files
inspect_docx.py     — Terminal viewer for output docx (flags Korean / equations / images)
compare_models.py   — Run the same source through multiple models and compare timing + Korean ratio
retry_failed.py     — Re-translate just the Korean paragraphs from a <output>.partial.docx snapshot
translate/
  client.py         — OpenAI-compatible LLM client (Ollama, OpenAI, vLLM, …)
  translator.py     — Core translation engine
  s3.py             — S3 file listing and download helpers
  agent/
    prompts.py      — All prompt builders (4 system prompts, all one sentence)
    pipeline.py     — Sequential pipeline (load → claims → body → abstract → write)
    nodes/          — Per-section node implementations
docs/
  PROMPTS.md        — How prompts flow through the pipeline
  PIPELINE.md       — High-level pipeline diagram
data/               — Input documents (git-ignored)
output/             — Translated documents + .log audit files (git-ignored)
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

S3 credentials live in `.env`:

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
uv run python main.py data/sample.docx

# Explicit output path
uv run python main.py data/sample.docx output/result.docx

# OpenAI
uv run python main.py data/sample.docx \
  --base-url https://api.openai.com/v1 \
  --api-key sk-... \
  --model gpt-4o
```

At startup the run prints a config banner so you can confirm what's about to run:

```
============================================================
  input            : data/sample.docx
  output           : output/sample_en.docx
  model            : gpt-oss:120b
  base_url         : http://localhost:11434/v1
  temperature      : 0.2
  max_tokens       : 4096
  body chunk chars : 1800
  batch_size       : 30
  font             : Times New Roman
  review           : True
============================================================
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--model` | `gpt-oss:120b` | Model name (e.g. `qwen3.5:122b`, `gemma4:31b`, `gpt-4o`) |
| `--base-url` | `http://localhost:11434/v1` | API base URL |
| `--api-key` | `ollama` | API key |
| `--temperature` | `0.2` | Sampling temperature |
| `--max-tokens` | `4096` | Max tokens per response (bump to 32 768 for reasoning models like qwen3.5) |
| `--batch-size` | `30` | Max paragraphs per review batch |
| `--font` | `Times New Roman` | Output font |
| `--delay` | `0.0` | Seconds between API calls |
| `--no-review` | — | Skip the post-translation review pass |
| `--quiet` | — | Suppress progress output |
| `--log` | `<output>.log` | Per-document audit log path |

### Switching models

Use any model your Ollama (or OpenAI-compatible endpoint) can serve:

```bash
# .env (persistent across runs)
LLM_MODEL=qwen3.5:122b
LLM_MAX_TOKENS=32768          # reasoning models burn tokens on chain-of-thought
LLM_CHUNK_CHARS=1800          # body chunk char cap
LLM_CONTEXT_BACK=2            # trailing-context window (chunks)

# Or one-shot override on the CLI
uv run main.py sample.docx --model qwen3.5:122b --max-tokens 32768
```

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
WORKERS = 4          # files processed in parallel (separate worker processes)
BATCH_SIZE = 10      # max paragraphs per review batch
REVIEW = True        # post-translation review pass per section
FONT = "Times New Roman"
DELAY = 0.0          # seconds between API calls within one file
```

With a local Ollama model, `WORKERS > 1` does not reduce wall-clock time per file — Ollama serializes inside the model. Increase `WORKERS` when using an API provider that supports concurrent requests.

## Inspecting output

After a run, audit the output docx from the terminal:

```bash
# Per-paragraph view with KO / EQ / IMG / HDR flags
uv run inspect_docx.py output/sample_en.docx

# Summary only (totals + Korean character ratio)
uv run inspect_docx.py output/sample_en.docx --summary

# Just the paragraphs that still contain Korean (zero on a clean run)
uv run inspect_docx.py output/sample_en.docx --korean-only
```

## Comparing models

Run the same source through multiple models in sequence and get a summary table of timing + Korean leak ratio:

```bash
uv run compare_models.py data/sample.docx \
    --models qwen3.5:122b gemma4:31b gpt-oss:120b
```

Output:

```
==============================================================================
  COMPARISON SUMMARY
==============================================================================
  model                 status        wall   paras  KO_paras    KO_%
  qwen3.5:122b          ok          1342.0s     91         0   0.00%
  gemma4:31b            ok           220.0s     91         2   0.04%
  gpt-oss:120b          ok           485.0s     91         0   0.00%
==============================================================================
```

Each run also writes its own output file: `output/sample_<model>.docx`.

## Translation Pipeline

Each document flows through a sequential pipeline (claims first so claim terminology seeds the body):

```
load → classify → apply_static
     → chunk_claims → translate_claims → review_claims
     → chunk_body   → translate_body   → review_body
     → chunk_abstract → translate_abstract → review_abstract
     → write
```

See `docs/PIPELINE.md` for the diagram and `docs/PROMPTS.md` for how the prompts attach.

### Slim prompts

Every translation system prompt is **one sentence** mentioning "USPTO style":

| Prompt | Used by | Approximate size |
|---|---|---|
| `BODY_SYSTEM`     | body chunks                          | 246 chars |
| `SEGMENT_SYSTEM`  | equation-fragment translation        | 189 chars |
| `CLAUSE_SYSTEM`   | parameter legend clauses             | 228 chars |
| `ABSTRACT_SYSTEM` | abstract                             | 191 chars |
| `BULK_CLAIMS_SYSTEM` | claims (single bulk call)         | 321 chars |
| `_REVIEW_SYSTEM`  | review decide + revise               | 261 chars |

The USPTO drafting rules (bracket policy, FIG. casing, antecedent basis, "comprising" discipline, parameter-legend layout, reference numerals) are enforced by **deterministic postprocess sweeps in code**, not by long prompts. Switching models doesn't require re-tuning the prompt — the code keeps the USPTO layout shape regardless.

### Glossary handoff

Claims run first. The bulk-claims response includes a `===== GLOSSARY =====` block of Korean→English noun-phrase pairs; those seed `state["glossary"]` as canonical (claim terms never get overwritten later). Body and abstract chunks read that glossary in every prompt and can extend it with new terms via the same trailer convention. `setdefault` semantics throughout.

### Trailing context (body only)

Each body chunk's prompt gets the last `LLM_CONTEXT_BACK` already-translated chunks prepended as a read-only context block. Helps with antecedent references ("the layer", "the device"), pronoun resolution, and parallel structure across paragraphs. Default: 2 chunks. Set `LLM_CONTEXT_BACK=0` to disable.

Claims (single bulk call) don't need this — the model sees every claim at once.

### Zero-tolerance Korean guard

Every translator path runs `_contains_hangul(text)` on each chunk's result and re-tries once with a corrective follow-up message if Korean is detected. At write time a strict guard scans every paragraph in the final docx; if **any** paragraph still contains Hangul, the write is **aborted with `RuntimeError`** listing the offending paragraphs. The output file is only saved when every paragraph is English.

When the guard aborts, the in-memory document is still written to a sibling `<output>.partial.docx` snapshot so the failed paragraphs can be retried without re-translating the whole document.

To diagnose a failed run:

```bash
# Why did chunks fail?  (DIAG lines show raw LLM responses for failed chunks)
grep -E "DIAG|FAIL|empty|KOREAN" output/sample_en.log
```

### Retrying just the failed chunks

After a failed run that left `output/sample_en.partial.docx` behind, re-translate **only** the paragraphs that still contain Korean (typically a tiny fraction of the document) and finalize the output:

```bash
uv run retry_failed.py output/sample_en.partial.docx
```

The script walks the partial, finds every Hangul-containing paragraph, sends each through the same body prompt used during the original run, and writes the translation back in place. If every paragraph clears, it saves the final `output/sample_en.docx` and removes the partial. If some paragraphs are still Korean, it re-saves the partial (now containing fewer failures) so you can retry with a stronger model or higher `--max-tokens`.

```bash
# Same flags as main.py: --model, --base-url, --max-tokens, --temperature, --font, --delay
uv run retry_failed.py output/sample_en.partial.docx --model qwen3.5:122b --max-tokens 32768
uv run retry_failed.py output/sample_en.partial.docx --output output/sample_final.docx
uv run retry_failed.py output/sample_en.partial.docx --keep-partial   # keep the partial even after success
```

### Review pass (per section)

After each section is translated, a two-step review runs:

1. **Decision** — sends Korean/English pairs + glossary to the LLM. Returns `{ "needs_revision": bool, "issues": [...] }`. Every flagged issue is printed/logged.
2. **Revision** — if issues exist, a second call returns only the paragraphs that need changes. Each applied revision is logged as a `[REVISION ...]` block with BEFORE/AFTER text so you can audit:

```
[REVISION claims chunk-claim-7 (claim 7)]
  ISSUE 1: Claim 7 'first semiconductor die comprises a second substrate' is confusing
  BEFORE: 7. A semiconductor package, comprising: a first die comprising a second substrate; ...
  AFTER : 7. A semiconductor package, comprising: a first die comprising a first substrate; ...
```

After a run, audit revisions:

```bash
grep -A 4 "REVISION" output/sample_en.log
```

Disable with `--no-review` (CLI) or `REVIEW = False` (batch).

## Logs

Every run writes `output/<stem>.log` next to the docx, containing:

- Startup config (model, max_tokens, chunk size, …).
- Per-chunk progress (heartbeat every 10 chunks; failures always).
- `DIAG:` lines for any chunk that failed both attempts (raw LLM response preview).
- `[REVIEW <section>] Issue: …` for every quality issue the reviewer raised.
- `[REVISION <section> chunk-N] BEFORE/AFTER` blocks for every applied revision.
- `WARNING: BODY translation unavailable for chunk(s): …` if any chunks failed.
- The final RuntimeError + paragraph list if the strict guard aborted the save.
