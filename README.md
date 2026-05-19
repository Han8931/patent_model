# Patent Model

Korean → English patent application translator. Reads Korean `.docx` files and produces English `.docx` output in USPTO patent application style.

## Pipeline

```
   1. claims      single bulk LLM call (all claims at once)
                  → validate against 【청구항 N】 anchors → retry missing → review pass
                  → returns English claims + Korean→English glossary
   2. abstract    one call using the glossary
   3. description paragraph-by-paragraph using the glossary
                  + a rolling 3-paragraph context window
```

Failure tolerance: a per-file log records every error (claims/abstract failures, individual paragraph failures with their Korean snippet). A failed paragraph leaves its Korean text in place rather than aborting the whole file.

## Requirements

- Python 3.13+
- [uv](https://github.com/astral-sh/uv)
- [Ollama](https://ollama.com), or any OpenAI-compatible API

## Setup

```bash
uv sync

# Pull a model (Ollama example)
ollama pull gpt-oss:120b

# Configure the LLM endpoint
cp .env.example .env
# edit .env — LLM_MODEL, LLM_BASE_URL, LLM_API_KEY, etc.
```

## Translate a single file

```bash
uv run python main.py data/foo.docx
# writes:  output/foo_en.docx
# log:     output/foo_en.log
```

Optional flags:

| Flag              | Default            | Description |
|-------------------|--------------------|-------------|
| `output` (positional) | `output/<stem>_en.docx` | Explicit output path |
| `--suffix S`      | (none)             | Insert `S` before `.docx` → `output/<stem>_en<S>.docx`. Useful for versioning runs |
| `--model M`       | `LLM_MODEL` from `.env` | Override the model for this run only |
| `--agent`         | off (heuristic)    | Use the agentic pipeline (see below) |

Examples:

```bash
# explicit output path
uv run python main.py data/foo.docx output/foo_my_version.docx

# version a run with a suffix and a different model
uv run python main.py data/foo.docx --suffix _qwen --model qwen3.5
# writes:  output/foo_en_qwen.docx + .log

# agentic pipeline (LLM-driven section + claim classification)
uv run python main.py data/foo.docx --agent --suffix _agent
```

The CLI prints a banner before running:

```
input:  data/foo.docx
output: output/foo_en.docx
model:  gpt-oss:120b @ http://localhost:11434/v1
mode:   heuristic

[1/3] Translating claims (40 paragraphs, single LLM call)…
      got 20 claim(s); glossary=37 entries
[2/3] Translating abstract…
[3/3] Translating description (63 paragraphs, context window=3)…
      10/63
      ...
wrote output/foo_en.docx

elapsed: 142.3s
```

## Batch mode

Translate every `.docx` under a directory in parallel:

```bash
uv run python batch.py                              # data/, 1 worker
uv run python batch.py --worker 5
uv run python batch.py --worker 5 --suffix _v1
uv run python batch.py --data-dir data/batch3 --worker 3 --model qwen3.5 --agent
```

Flags:

| Flag              | Default | Description |
|-------------------|---------|-------------|
| `--data-dir DIR`  | `data`  | Directory scanned recursively for `.docx` files |
| `--worker N`      | `1`     | Parallel worker count (ThreadPool — useful when the LLM backend supports concurrent requests) |
| `--suffix S`      | (none)  | Suffix added to every output stem |
| `--model M`       | `.env`  | Override model for the whole batch |
| `--agent`         | off     | Use the agentic pipeline for every file |

Per-file output is prefixed with `[i/N] <filename>` so interleaved logs from concurrent workers stay readable. Final line: `done — K/N succeeded in Xs` (exit code 1 if anything failed).

> **Note:** with local Ollama serving a single model, `--worker > 1` doesn't speed things up — the model handles one request at a time. Concurrent workers only help when the backend supports parallel requests (OpenAI, vLLM, multi-GPU Ollama, etc.).

## Heuristic vs Agent mode

| Step                  | Heuristic (default) | Agent (`--agent`) |
|-----------------------|---------------------|-------------------|
| Section detection     | Korean bracket regex (`[청구범위]`, `[요약서]`, …) + Hangul-signature lookup | LLM labels every paragraph HEADER / CLAIM / ABSTRACT / DESCRIPTION / BLANK |
| Media detection       | Structural (`<w:drawing>`, `<m:oMath>`) — same in both modes | Same — runs before the LLM so it never reclassifies media from text alone |
| Claim dep/ind         | LLM infers per claim during translation | Separate LLM classification pass; the dep/ind map is included in the bulk-claims prompt |
| Translation           | Same prompts; claims-first → glossary → abstract → description | Same prompts; the bulk claims call also sees the dep/ind annotation |

Agent mode runs **two extra LLM calls per file** before translation: one for section classification, one for claim dep/ind. The translation phase is identical.

## Output formatting

- Times New Roman is forced on every translated run (all four Word font slots: ascii / hAnsi / eastAsia / cs). Untranslated Korean falls back to Times New Roman too rather than the source's Korean font.
- Claims are post-processed into USPTO line layout: line break after every `;` and `:`, with a single tab indent on continuation lines.
- Section headers (`[청구범위]`, `[요약서]`, …) are rewritten in place with their English equivalents (`[CLAIMS]`, `[ABSTRACT]`).
- Images and equations pass through untouched. Paragraphs that mix text *and* an image/equation are left in Korean with a log warning so a human can do that one manually.

## LLM configuration

Set in `.env`:

```ini
LLM_MODEL=gpt-oss:120b
LLM_BASE_URL=http://localhost:11434/v1   # Ollama default
LLM_API_KEY=ollama                       # any string for Ollama; real key for cloud APIs
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=4096
```

`--model` overrides `LLM_MODEL` only; the rest comes from `.env`.

The claims call (bulk + retry + review) internally uses a higher `max_tokens=16384` so a long claims block can't get truncated mid-text. The description and abstract calls use the `LLM_MAX_TOKENS` default.

## Output format (LLM responses)

All LLM responses are plain text — no JSON anywhere. The claims call uses a `---GLOSSARY---` sentinel:

```
1. A semiconductor device, comprising:
   a substrate;
   ...

2. The semiconductor device of claim 1, wherein ...

---GLOSSARY---
반도체 다이 -> semiconductor die
기판 -> substrate
```

The parser is tolerant of partial truncation (a cut-off last claim still parses everything before it) and ` ``` ` fences if the model adds them.

## Project structure

```
main.py            — single-file CLI
batch.py           — batch CLI with --worker, --suffix, --model, --agent
download.py        — pull .docx files from S3
preprocess.py      — strip [NNNN] paragraph numbering from raw .docx files
translate/
  __init__.py      — re-exports translate_file, translate_file_agent, resolve_output_path
  client.py        — OpenAI-compatible LLM client
  prompts.py       — all prompts + tuning constants (CLAIMS_MAX_TOKENS, DESCRIPTION_CONTEXT_WINDOW)
  docx_io.py       — section detection, paragraph scan, run/paragraph rewrite, apply_*
  translator.py    — heuristic pipeline: translate_file, translate_claims/_abstract/_paragraph
  agentic.py       — agentic pipeline: translate_file_agent + LLM classifiers
  s3.py            — S3 helpers used by download.py
data/              — input documents (git-ignored)
output/            — translated documents + per-file .log (git-ignored)
```

## Logs

Every `translate_file` writes a per-file log at `<output_stem>.log` (so `output/foo_en_v1.docx` pairs with `output/foo_en_v1.log`). Each log records:

- the resolved model + base URL,
- counts of claims / glossary entries / paragraphs,
- every paragraph failure with its index and a 160-char Korean snippet,
- review pass revisions,
- mixed text-and-media paragraphs that were left in Korean.

To triage a batch run, grep the output dir for errors:

```bash
grep -lE '\[ERROR\]|\[WARNING\]' output/*.log
```
