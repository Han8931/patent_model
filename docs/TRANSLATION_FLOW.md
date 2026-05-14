# Translation Flow

```
input.docx
   │
   ▼
┌──────────┐
│   load   │  open the docx in memory; snapshot every <m:oMath> signature
└─────┬────┘  for the post-write integrity check
      │
      ▼
┌──────────┐
│ classify │  walk EVERY paragraph (incl. inside tables) and tag each as
└─────┬────┘    text / image / claim_header / section_header / blank
      │        + detect math, drawings, mixed text+math
      ▼
┌──────────────┐
│ apply_static │  deterministic header rewrites: [발명의 명칭]→TITLE OF INVENTION etc.
└──────┬───────┘
       │
       ▼
       split into three section flows (sequential — glossary builds up)
       │
       ├─ BODY ──────────────────────────────────────────────────────────────┐
       │                                                                     │
       │  chunk_body                                                          │
       │   ├─ greedy-join paragraphs (':' / ',' continuation, hard 1.8k char  │
       │   │   cap)                                                           │
       │   ├─ strip leading [NNN] paragraph IDs → store on chunk              │
       │   └─ renumber inline [EQUATION] → [EQUATION_1..N]                    │
       │                                                                     │
       │  translate_body — ROUTE PER CHUNK:                                   │
       │   │                                                                  │
       │   ├─ pure prose ───────► one LLM call per chunk                     │
       │   │                                                                  │
       │   └─ has math OR ──────► PER-PARAGRAPH IN-PLACE                     │
       │      legend (여기서…)    walk each <w:p> in the chunk:               │
       │                          • pure-math <w:p>: SKIP (math untouched)    │
       │                          • text+math:   build [EQUATION_N] markers,  │
       │                                         translate, replace_text →    │
       │                                         interleave English around    │
       │                                         the math at source XML pos.  │
       │                          • legend text: split '여기서, A는…, B는…'   │
       │                                         per-symbol; one LLM call     │
       │                                         per clause; join with ';\t'  │
       │                          set chunk.applied_in_place=True             │
       │                                                                     │
       ├─ ABSTRACT ─────────────────────────────────────────────────────────┐
       │                                                                     │
       │  chunk_abstract  → one chunk for the entire abstract                 │
       │  translate_abstract → one LLM call                                  │
       │                                                                     │
       ├─ CLAIMS ───────────────────────────────────────────────────────────┐
       │                                                                     │
       │  chunk_claims                                                        │
       │   ├─ group paragraphs by claim header                                │
       │   ├─ number [EQUATION_N] within each claim                          │
       │   └─ pre-classify each claim (deterministic):                       │
       │       claim_kind ∈ {device, method, crm, system},                   │
       │       is_independent, parent_claim_nums, multi_parent_kind          │
       │                                                                     │
       │  translate_claims — TWO STAGES:                                     │
       │   ├─ Phase 1: INDEPENDENTS (claim-number order)                     │
       │   │    pick per-kind prompt → translate → extract noun_phrase       │
       │   │    + actor_phrase into a PreambleSpec keyed by claim_num        │
       │   │                                                                  │
       │   └─ Phase 2: DEPENDENTS (source order)                              │
       │        look up parent's PreambleSpec → build literal preamble       │
       │        ('The <noun_phrase> of claim N')                              │
       │        method: pick 'wherein' vs 'further comprising' from Korean   │
       │        send prompt with PREAMBLE LOCK; reattach standardized form   │
       │                                                                     │
       │ (each section may also run an optional review_decide → revise loop) │
       ▼
┌──────────┐
│  write   │  for each translated chunk:
└────┬─────┘    • applied_in_place chunks: already done, skip
     │          • everything else: replace_text in head paragraph;
     │            tab-indent after ';' breaks
     │
     │   then SAFETY GATES, in order:
     │     1. Korean-char ratio > 40%? RAISE — refuse to save a fake output
     │     2. Equation integrity report: any <m:oMath> lost/moved/duplicated?
     │        log warnings (signature + paragraph index, no source content)
     │
     ▼
   doc.save(output.docx)
```

## What flows through the pipeline

A single `TranslationState` dict, accumulating:

- `doc` — the open python-docx Document
- `records` — every paragraph with its classification
- `chunks_body / _abstract / _claims` — translation units
- `glossary` — Korean → English term map, **also** the antecedent-basis ledger ("these terms are already introduced — use 'the'")
- `math_snapshot` — OMML signatures captured at load
- `claim_preamble_specs` — locked-in noun phrases from Phase 1 independents

## The three guarantees

1. **Equations never move.** Per-paragraph in-place + integrity audit catches any drift.
2. **No silent failures.** No upfront copy + Korean-ratio gate + cleanup-on-exception. You get either a real translation or no output file.
3. **Mechanical structure.** Paragraph IDs, claim preambles, "further comprising", tab indents, per-parameter clauses — all enforced both by prompt rules and by deterministic regex/structural passes.
