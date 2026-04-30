# Audit — Improvement & Speed-up Opportunities

Reviewed every node in `translate/agent/`. Findings ranked by likely impact.

---

## A. Speed (biggest wins first)

### A1. Parallelise body chunks within a section — **5–10× faster**
`translate_body` translates body chunks one at a time, sequentially, even though each call is independent. Body sections often have 50–150 chunks. Wrapping the loop in `concurrent.futures.ThreadPoolExecutor(max_workers=8)` (or `asyncio` if the OpenAI SDK's async client is wired up) cuts wall time roughly proportional to worker count, bounded by the LLM provider's rate limit.

Risk: terminology consistency — chunks finish out of order, so the glossary built mid-section isn't visible to in-flight chunks. Two ways to handle: (a) translate sequentially within a section but parallelise across body chunks of the *same* prompt prefix; (b) accept some glossary lag and let the review pass clean it up.

### A2. Parallelise claims — **20× for documents with 20 claims**
Same idea, easier to apply: each claim is fully independent (the only cross-claim signal is the "independent claim categories" map, used only for dependent claims to pick `<category>`). Process independent claims first (sequentially or in one batch), then dependent claims in parallel.

### A3. Drop the artificial `delay` between calls — **noticeable on local Ollama**
`translate_body` and `translate_claims` `time.sleep(delay)` between calls. On a local Ollama instance there's no rate limit to respect; the sleep is dead time. Default it to `0.0` and only set it when targeting a hosted API.

### A4. Cache the prompt prefix — **15–25% throughput**
Every call rebuilds the system prompt, which is large (`COMMON_SYSTEM` + section rules + glossary). For an OpenAI-compatible provider that supports prompt caching (Anthropic, OpenAI), passing the system message identically across calls within a section unlocks server-side caching automatically. The current code already does this — but `_system_with_glossary` interleaves the glossary into the system prompt, which busts the cache every time the glossary changes. Move the glossary into the *user* message instead so the system prompt is invariant across an entire section.

### A5. Use streaming for review-decide — **noticeable latency cut**
Review-decide is a small JSON output but currently waits for the full completion. Streaming and short-circuiting on first `"needs_revision": false` saves ~0.3–1s per section.

### A6. Skip review when nothing controversial happened
Currently `review_decide_*` runs on every section with ≥2 chunks. Add a heuristic to skip the decide call when the glossary saw no conflicts and no chunk produced an unusually short translation — saves one LLM call per section.

---

## B. Quality

### B1. Glossary is built during translation but never fed *back* into earlier sections
Body terms inform abstract and claims, but if the abstract clarifies a term the body got wrong, that fix doesn't propagate. Two passes: translate everything → review with full glossary → revise. Costs more LLM calls but materially improves consistency.

### B2. Per-claim translation loses cross-claim context
`translate_claims` calls the LLM once per claim — the LLM can't see claim 5's preamble when translating claim 6's "wherein" clause. The `independent_categories` dict patches one piece of this, but full claim text from claim N–1 isn't visible. Options: (a) include the previously-translated claim in the user message as context (small token cost); (b) batch ~5 claims per call (smaller-context LLMs may struggle).

### B3. Body chunking heuristic is too narrow
Right now we merge only when a paragraph ends with `:` or `,`. Korean technical writing often splits sentences after `및` (and), `또는` (or), trailing connectives like `있고,` `포함하고,` — none of these get merged. Result: short, choppy English. Either expand the regex set or run a lightweight Korean sentence-end detector (`다.`, `이다.`, `있다.` → terminal; everything else → continuation).

### B4. Pure-equation paragraphs are never translated even when surrounded by referenced text
A paragraph that's just `<m:oMath>` is correctly preserved, but the LLM never sees that an equation lives there when translating the surrounding chunks. So translations referring to the equation use vague phrasing. Fix: insert a placeholder marker `[EQUATION_N]` into the chunk text when an equation paragraph sits between two text records, and tell the LLM "this is an inline equation, refer to it as 'the following equation' or similar."

### B5. Reference-numeral consistency isn't enforced post-hoc
Patents are full of "기판(100)" → "substrate 100" rewrites. The prompt requests this but the LLM occasionally writes "the substrate (100)" or just "the substrate". A deterministic post-pass that finds bare Korean numerals next to noun phrases and ensures they're rendered consistently would clean this up. Cheap and reliable.

### B6. Claim antecedent basis isn't validated
Claims must introduce with `a/an` and refer back with `the`. The prompt requests this but doesn't enforce it. A post-pass parser (or a focused review prompt that *only* checks antecedent basis) would catch most violations. Even just regex-flagging `the <noun>` that appears before any `a <noun>` of the same head noun gives a useful signal.

### B7. Review only fires when ≥2 chunks
The threshold is `len(buffer) >= 2`. Single-chunk sections (often the abstract) skip review entirely. Lower to `>=1`, or run a lightweight self-check on every section.

### B8. Hardcoded fallbacks in translation nodes hide failures
`translate_body` falls back to the Korean text on any exception, with `verbose` print only. In a long run an exception-driven fallback can leave dozens of paragraphs in Korean and the user only notices at the end. Two fixes: collect failures into state and report at the end; raise on a threshold (`>5%` failure rate aborts the run).

---

## C. Architecture / robustness

### C1. State is mutated in place (not idiomatic LangGraph)
Several nodes mutate the `Chunk` objects (`chunk.translation = ...`) and return `{"chunks_body": chunks}`. LangGraph expects pure-functional updates. Mutation works because dict update is shallow, but if you ever want to checkpoint state or replay graphs (`graph.get_state` / human-in-the-loop), this will break. Switch to building new Chunk dataclasses with `dataclasses.replace`.

### C2. Recursion limit set to 50 in `translator.py`
`config={"recursion_limit": 50}` — a hard-coded magic number. The current graph only has ~14 nodes. The limit is too generous (hides infinite-loop bugs) and not parameterised. Set it based on the actual max walk length.

### C3. No checkpointing — long runs that crash lose all work
LangGraph supports checkpointers (`MemorySaver`, `SqliteSaver`). For a 200-paragraph document with the model running ~20 minutes, a crash mid-claims means redoing body+abstract. With a checkpointer keyed on `input_path`, we resume from the last completed node. Worth it for long documents.

### C4. The `client` lives inside state, which makes state non-picklable
LangGraph checkpointers serialise state. The `LLMClient` and `Document` objects in state can't be pickled. Workaround: pass the client via a closure (factory function captures `client`, returns nodes), and pass `doc` via a side channel. This lets checkpointing work and removes the client-as-state coupling.

### C5. `chunk_body` runs even when no body sections exist
Chunking is cheap, but the structure means every section runs even when it has zero work. With a conditional edge (`has_body_chunks` → translate vs skip), we avoid unnecessary node entries in the trace.

### C6. No retry/backoff on LLM failures
A single 429 from the LLM provider currently fails the chunk → falls back to Korean. A simple `tenacity` retry decorator on `client.complete` with exponential backoff handles transient errors. Cheap, high value.

### C7. Glossary has no upper bound
After translating a long document, the glossary can hold hundreds of terms. They all get injected into every prompt, eating tokens and reducing the model's effective context for actual translation. Cap at ~80 most-frequent or most-recent terms.

### C8. Word-count footer is brittle
`insert_para_after` uses XML manipulation that bypasses python-docx's API. If the doc has section properties, headers, or footers near the abstract, this can produce malformed XML. Use `doc.paragraphs[i].insert_paragraph_before(...)` from python-docx where possible.

### C9. No tests
Adding even 3–4 tests on the deterministic parts (`chunk_body`, `chunk_claims`, `classify`) protects against regression as you keep tweaking. The agent has enough surface area now that ad-hoc smoke tests aren't catching things.

---

## Recommended order

Maximum bang-per-effort:

1. **A2 (parallelise claims)** — biggest speed win, low risk because claims are independent.
2. **A3 (drop default delay)** — one-line change, immediate speedup on local Ollama.
3. **C6 (retry/backoff)** — robustness for hosted APIs, cheap to add.
4. **A4 (move glossary out of system prompt)** — restores prompt cache, 15–25% throughput.
5. **B3 (better body chunking heuristic)** — quality win, no LLM cost.
6. **A1 (parallelise body)** — bigger speed win but needs careful glossary handling.
7. **C3 (checkpointing)** — only worth it if you're hitting crashes mid-run.
