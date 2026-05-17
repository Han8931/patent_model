# Prompt flow

How the prompts in `translate/agent/prompts.py` are used by the pipeline.
Pairs with `PIPELINE.md` (high-level flow) and `TRANSLATION_FLOW.md`
(per-paragraph mechanics).

## Design intent

The prompts are **deliberately minimal** — one or two sentences each, all
starting with "Translate ... in USPTO style". The heavy lifting that long
prompts used to do (USPTO drafting rules, bracket policy, antecedent
basis, FIG. casing, "comprising" discipline) is now done by deterministic
**postprocess sweeps** in `docx_utils.py` and `nodes/translate_claims.py`.
The model only needs the high-level task; the code enforces the layout.

This trade has two benefits:

1. Far fewer tokens per LLM call — faster, cheaper.
2. The rules are now in code, so they apply to every model the user picks
   (Qwen, GPT-OSS, GPT-4o, Claude, etc.) without re-tuning the prompt for
   each one's idiosyncrasies.

## The six system prompts

All defined in `translate/agent/prompts.py`. None imported from anywhere
else.

| Constant | Used by | Where the LLM sees it | What downstream parser expects |
| --- | --- | --- | --- |
| `BODY_SYSTEM` | `build_body_messages` | body chunks | plain text + optional `===== GLOSSARY =====` trailer |
| `SEGMENT_SYSTEM` | `build_segment_messages` | fragment between two `[EQUATION_N]` markers | plain text |
| `CLAUSE_SYSTEM` | `build_clause_messages` | one parameter legend clause (e.g. translating `α는 위상차 값`) | `'<symbol> is <description>'` |
| `ABSTRACT_SYSTEM` | `build_abstract_messages` | abstract section | plain text + optional `===== GLOSSARY =====` trailer |
| `BULK_CLAIMS_SYSTEM` | `build_claims_bulk_messages` | every claim in one call | banner-delimited claims + `===== GLOSSARY =====` trailer |
| `_REVIEW_SYSTEM` | `build_decision_messages` / `build_revision_messages` | post-translation review pass | JSON only |

Every translation prompt names **USPTO style** explicitly so the model
defaults to the right register on the first try.

## Pipeline → prompts mapping

```
┌──────────────────────────┐
│ load → classify → static │   (no LLM calls — load, classify, apply_static)
└────────────┬─────────────┘
             │
   ┌─────────▼──────────────────────┐
   │   CLAIMS section               │  1 LLM call → BULK_CLAIMS_SYSTEM
   │   chunk_claims + translate     │  Response parsed by:
   │                                │    parse_claims_bulk_response   → {claim_num: english}
   │                                │    parse_claims_bulk_glossary   → {ko: en}  ← seeds glossary
   └─────────┬──────────────────────┘
             │
   ┌─────────▼──────────────────────┐
   │    BODY section                │  N LLM calls (one per chunk).
   │    chunk_body + translate_body │  Routing inside translate_body:
   │                                │    pure prose            → build_body_messages
   │                                │    has [EQUATION_N] etc. → paragraph_translator → segment / clause builders
   │                                │  parse_translation_with_glossary() extracts the
   │                                │  GLOSSARY trailer; new terms extend state.glossary
   │                                │  via setdefault (claim terms are immutable).
   └─────────┬──────────────────────┘
             │
   ┌─────────▼──────────────────────┐
   │  ABSTRACT section              │  1 LLM call → ABSTRACT_SYSTEM
   │  chunk_abstract + translate    │  parse_translation_with_glossary() same as body
   └─────────┬──────────────────────┘
             │
   ┌─────────▼──────────────────────┐
   │  REVIEW pass (optional)        │  Per section, 1 decide call → _REVIEW_SYSTEM
   │  --no-review skips this        │  If "needs_revision": one revise call → _REVIEW_SYSTEM
   └─────────┬──────────────────────┘
             │
        ┌────▼────┐
        │  write  │   (no LLM calls — splice translations back into the docx)
        └─────────┘
```

## Glossary flow

The glossary is the cross-section mechanism for terminology consistency.

```
state.glossary = {}                      # starts empty

translate_claims runs first
  ─ model returns ===== GLOSSARY =====   ko→en pairs after the last claim
  ─ parse_claims_bulk_glossary() extracts them
  ─ state.glossary.setdefault(ko, en)   ← claim terms become canonical

translate_body runs per chunk
  ─ injects state.glossary into the system message
  ─ model returns ===== GLOSSARY =====   any NEW pairs
  ─ parse_translation_with_glossary() extracts them
  ─ state.glossary.setdefault(ko, en)   ← body can extend, never overwrite

translate_abstract runs once
  ─ same pattern; setdefault only
```

Claims-first ordering is intentional: the claims define the legal anchor
noun phrases ("a semiconductor device" vs "an apparatus"), and the body
must describe what the claims claim using the **same English wording**.
Seeding the glossary from claims keeps that consistency automatic.

## What the model is NOT told (because postprocess handles it)

Every rule in this list used to live in a long system prompt. They were
moved to code so the model can use its own judgment for the actual
translation while the code keeps the output USPTO-shaped.

| USPTO concern | Where it's enforced |
| --- | --- |
| Strip markdown (`**bold**`, bullet lists, `---` rules) | `_strip_markdown` in `docx_utils.py` |
| Reference numerals `(100)` → `100` | `_strip_reference_parens` |
| Articles on plurals (`an layers` → `layers`) | `_strip_articles_on_plurals` |
| `further comprising` instead of `further including` | `_enforce_further_comprising` |
| Letter-list markers `a)`, `(b)`, `iii)` in claim elements | `_normalize_claim_breaks` |
| Indent after `;` and `:` in claim element lists | `_indent_after_colon` + `_normalize_claim_breaks` |
| `[EQUATION_N]` position in body output | `paragraph_translator` (per-paragraph translation, math stays at source XML position) |
| Paragraph indent on claim paragraphs | `clear_paragraph_indent` in writer |
| Korean leaking through (`_contains_hangul`) | Korean-check + retry in `translate_body` / `translate_abstract` |

## Builder reference (every function in `prompts.py`)

```python
# Body — one chunk per call.
build_body_messages(chunk_text, glossary, equation_context=None)
build_body_retry_messages(*, previous_messages, problem, chunk_text)

# Sentence-level — fragments inside equation-bearing paragraphs.
build_segment_messages(korean_segment, glossary)
build_clause_messages(symbol, korean_clause, glossary)

# Abstract — one call.
build_abstract_messages(chunk_text, glossary)

# Claims — single bundled call for ALL claims.
build_claims_bulk_messages(claims)                  # claims: list[(claim_num, ko_text)]
parse_claims_bulk_response(raw)                     # → {claim_num: english}
parse_claims_bulk_glossary(raw)                     # → {ko: en}

# Review — decide + revise per section.
build_decision_messages(section, pairs, glossary)
build_revision_messages(section, pairs, issues, glossary)

# Shared parser for body / abstract responses.
parse_translation_with_glossary(raw)                # → (translation, {ko: en})
```

Six small system-prompt strings plus eleven builder/parser functions. The
file is 288 lines total. No imports from any other prompt module —
`translate/agent/prompts.py` is the single source of truth.

## How to add a new translation step

Mirror the existing pattern:

1. Add a one-sentence `MYTHING_SYSTEM = "Translate ... in USPTO style. ..."`.
2. Write `build_mything_messages(text, glossary)` returning the standard
   `[{role: system, ...}, {role: user, ...}]` shape with
   `_glossary_block(glossary)` appended to the system content.
3. If the response should extend the rolling glossary, ask the model for a
   trailing `===== GLOSSARY =====` block and parse with
   `parse_translation_with_glossary`.
4. Add the node to `pipeline.py` or `graph.py`.

That's it — no class registration, no per-kind variants, no shared
"COMMON_SYSTEM" template to update.
