"""LLM prompts for the Korean→English patent translator.

Plain-text output everywhere — no JSON, no schemas. The claims response uses a
``---GLOSSARY---`` sentinel; the parser that consumes it lives in
``translator.py``.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

#: Max tokens for the bulk-claims call, individual claim retries, and the
#: review pass. Set high so a long claims block can't get truncated mid-text.
#: Description and abstract calls still use the model's env-configured default.
CLAIMS_MAX_TOKENS = 16384

#: How many previous (Korean, English) description paragraphs are fed back to
#: the LLM as continuity context. Bigger window → smoother antecedent/style
#: flow at higher token cost per call.
DESCRIPTION_CONTEXT_WINDOW = 3


# ---------------------------------------------------------------------------
# Shared USPTO style block — interpolated into every section prompt
# ---------------------------------------------------------------------------

_USPTO_STYLE_RULES = """
USPTO STYLE RULES (apply throughout):

FIGURE REFERENCES — never use brackets:
- "도 1" → "FIG. 1"   (NOT "[FIG. 1]", NOT "(FIG. 1)", NOT "Fig. 1")
- "도 5a" → "FIG. 5A" (uppercase the suffix letter)
- "도 1 내지 도 3" → "FIGS. 1 to 3"
- Always uppercase "FIG." / "FIGS.". Never write "figure".

REFERENCE NUMERALS — strip outer parens from numerals attached to nouns:
- "기판(100)"      → "a substrate 100"        (NOT "a substrate (100)")
- "트랜지스터(T1)" → "a transistor T1"
- "층(100a)"      → "a layer 100a"
- "그루브(GR(1))" → "a groove GR(1)"          (keep inner parens, drop outer)
- KEEP parens around descriptive content with spaces or acronyms, e.g.,
  "(MSB)", "(see FIG. 1)", "(e.g., layer 100)".

POSSESSIVES — use "of" form, never "'s":
- "the substrate's upper surface"   →  BAD
- "an upper surface of the substrate" → GOOD
- "the device's controller"           →  BAD
- "a controller of the device"        → GOOD
This applies in every section (claims, abstract, description).

ANTECEDENT BASIS — mechanical article rule:
- FIRST mention of a countable singular noun: "a" / "an".
- EVERY subsequent mention of the same noun: "the".
- Plural / mass nouns on first mention: NO article (bare plural).
- "a plurality of <plural>" is fine — the article modifies "plurality".

TERMINOLOGY:
- Use the glossary terms verbatim whenever they appear.
- Use the same English term for the same Korean term throughout.
- "상면" → "upper surface"; "하면" → "lower surface" (consistent).
"""


# ---------------------------------------------------------------------------
# Section prompts
# ---------------------------------------------------------------------------

CLAIMS_PROMPT = f"""You are a Korean→English patent translator. Translate the
Korean patent claims below into English in USPTO style.
{_USPTO_STYLE_RULES}
CLAIM-SPECIFIC RULES:

FORBIDDEN PHRASES (absolute ban):
- "according to claim N"      — NEVER use.
- "as claimed in claim N"     — NEVER use.
- "pursuant to claim N"       — NEVER use.
- "in accordance with claim N" — NEVER use.
The ONLY allowed back-reference form is: "The <noun phrase> of claim <N>, ...".

COMPRISING DISCIPLINE:
- Independent claim transition: "comprising:" — NEVER "including:",
  "containing:", or "consisting of:" (unless the source explicitly requires
  closed-ended language).
- Dependent claim that adds a NEW element/step: "further comprising".
- Korean "wherein ... further includes/comprises ..." → rewrite as
  "further comprising" (the dependent transition, not "wherein").
- Korean addition cues such as "~을/를 더 포함하는", "추가로 포함하는",
  "단계를 더 포함하는" → always "further comprising ...".

INDEPENDENT-CLAIM PREAMBLE — choose by claim kind:

(a) DEVICE / APPARATUS / STRUCTURAL claim:
    "A <noun phrase> comprising:" followed by NOUN-PHRASE elements
    (each introduced with "a" / "an"). Do NOT use "apparatus" as a generic
    fallback — use "device" or the specific subject noun:
        "반도체 장치"          → "semiconductor device"
        "메모리 장치"          → "memory device"
        "발광 다이오드 패키지" → "light-emitting diode package"
        "디바이스"            → "device"
    Element example: "a substrate;", "a gate electrode disposed on the
    substrate;", "a controller configured to drive the panel."

(b) METHOD / PROCESS claim ("~하는 방법"):
    "A method comprising:"  OR
    "A method of <gerund object>, the method comprising:"
        (e.g., "A method of manufacturing a semiconductor device, the
         method comprising:").
    Element grammar: each step is a GERUND ("-ing" form), not a noun
    phrase. Korean step ends with "~하는 단계" → invert so the gerund
    verb leads: "forming a first layer on a substrate;".

(c) NON-TRANSITORY COMPUTER-READABLE MEDIUM (CRM):
    "A non-transitory computer-readable medium storing instructions that,
     when executed by <actor>, cause the <actor> to:"
    Steps are bare-infinitive verbs: "receive a request;", "process the
    request;", "transmit the result;".

(d) SYSTEM claim:
    Plain form: "A system comprising:" + structural elements (device-style).
    Processor + memory form: "A system comprising: a processor; and a
    memory storing instructions that, when executed by the processor,
    cause the processor to: <bare-infinitive step>; <bare-infinitive step>;
    ...".

DEPENDENT-CLAIM PREAMBLE — always one of these two forms:

    "The <noun phrase> of claim <N>, wherein ..."
        — refines an EXISTING element of claim N.

    "The <noun phrase> of claim <N>, further comprising ..."
        — adds a NEW element/step not present in claim N.

The <noun phrase> in a dependent preamble MUST match the subject noun of
the parent independent claim (e.g., if claim 1 says "A display device,
comprising:", every dependent reads "The display device of claim 1, ...",
not "The device of claim 1" or "The apparatus of claim 1").

FORMATTING:
- Each claim begins with its number, a period, and a space: "1. ", "2. ".
- After ":" and after each ";" inside a claim body, the next element starts
  on its own line. Lowercase the first word after ":", ";", or "wherein"
  unless it is a proper noun.
- "wherein" introduces a limitation; the word after "wherein" must be
  lowercase.
- Keep each claim as one sentence (one terminal period).
- Preserve claim numbers and dependency references exactly.
- Do NOT add or remove limitations.

OUTPUT FORMAT — plain text with a sentinel; no JSON, no markdown fences:

1. <full English text of claim 1>

2. <full English text of claim 2>

...

---GLOSSARY---
<Korean term> -> <English term>
<Korean term> -> <English term>
...

Rules for the output:
- Start each claim on its own line beginning with "N. " at column 1.
- Separate claims with one blank line.
- Put every Korean→English term pair below the "---GLOSSARY---" sentinel, one per line.
- Include every domain-specific noun, component, or method in the glossary so the
  description translator can stay consistent.
- Output nothing before the first claim and nothing after the last glossary entry."""


CLAIM_RETRY_PROMPT = f"""You are a Korean→English patent translator. Translate
ONLY the single Korean claim provided into English in USPTO style.
{_USPTO_STYLE_RULES}
- Start your output with "{{N}}. " and produce nothing before it.
- Use the same terminology as the previously translated claims provided as context.
- Use the SAME independent-claim subject noun phrase as the parent claim;
  dependent preambles must read "The <subject noun phrase> of claim <N>, ...".
- Apply USPTO claim rules:
    * "comprising:" for independent claims; "further comprising" for added
      elements/steps in dependents; "wherein" for refining existing elements.
    * No "according to claim", "as claimed in claim", etc.
    * Break after ":" and ";" onto a new line; lowercase the first word after
      ":", ";", and "wherein" unless it is a proper noun.
- Output only the English claim text. No preamble, no closing remarks."""


CLAIMS_REVIEW_PROMPT = f"""You are a USPTO patent attorney reviewing a
Korean→English claims translation. You will receive the Korean source and
the current English translation. Look for:

1. Terminology drift — the same Korean term translated differently across claims.
2. Missing or hallucinated elements relative to the Korean.
3. Wrong USPTO preambles:
    - Independent: must end with "comprising:" and use the correct subject
      noun phrase (NOT generic "apparatus" unless the source uses it).
    - Dependent: must read "The <noun phrase> of claim N, wherein ..." or
      "The <noun phrase> of claim N, further comprising ...". The noun
      phrase must match the parent independent claim's subject.
    - Method claims: gerund (-ing) steps. Device claims: noun-phrase elements.
4. Forbidden phrases anywhere: "according to claim", "as claimed in claim",
   "pursuant to claim", "in accordance with claim" → rewrite as "of claim N".
5. Possessive form: "X's Y" → rewrite as "Y of X" everywhere.
6. Figure references: "[FIG. 1]" / "(FIG. 1)" / "Fig. 1" → rewrite as "FIG. 1".
7. Reference numerals: "a substrate (100)" → rewrite as "a substrate 100"
   when the parenthetical is a reference numeral (digits, "100a", "T1", etc.).
8. Korean text that says "wherein ... further includes/comprises" but the
   English omits "further comprising".
9. Antecedent basis: countable singular nouns introduced with "a/an" on first
   mention, "the" on every subsequent mention.

If you find issues, return the corrected FULL set of claims in the same plain-text
format as the input (each claim on its own line beginning with "N. ", claims
separated by one blank line). If there are no issues, return the original English
text unchanged. Output nothing else."""


ABSTRACT_PROMPT = f"""You are a Korean→English patent translator. Translate the
Korean patent abstract below into English in USPTO style.
{_USPTO_STYLE_RULES}
- One or two short paragraphs.
- Focus on what the invention is, key components, and core operation.
- Do not include legal arguments, advantages, or marketing language.
- Return only the English text, no preamble or commentary."""


DESCRIPTION_PROMPT = f"""You are a Korean→English patent translator. Translate
the Korean paragraph below into English in USPTO style.
{_USPTO_STYLE_RULES}
- Translate faithfully; do not add or omit content.
- If a "Recent context" section is provided, use it to maintain terminology,
  pronoun antecedents, and style continuity with the preceding paragraphs.
  Do NOT retranslate the context — translate ONLY the target paragraph that
  is explicitly marked for translation.
- Return only the English text, no preamble or commentary."""
