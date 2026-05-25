"""Prompt definitions and builders for the translation agent."""

from __future__ import annotations

from dataclasses import dataclass
import re

# ---------------------------------------------------------------------------
# Shared rule blocks
# ---------------------------------------------------------------------------

_BRACKET_RULES = (
    "\n"
    "BRACKET / BRACE TRANSLATION POLICY:\n"
    "1) PARAGRAPH-ID MARKERS — any square-bracketed number from 1 to 5 digits at\n"
    "   the START of a paragraph is a paragraph ID. Examples: '[1]', '[12]',\n"
    "   '[001]', '[002]', '[0016]', '[0123]'. They MUST appear verbatim at the\n"
    "   very start of the corresponding output paragraph.\n"
    "   - DO NOT translate, paraphrase, remove, renumber, or reformat them.\n"
    "   - DO NOT split, merge, or move them inside the sentence.\n"
    "   - Preserve the bracket characters '[' and ']' exactly — never '(001)',\n"
    "     never '[001번]', never 'Paragraph 1', never '<001>'.\n"
    "   - If the source has 3-digit IDs ('[001]'), keep them 3-digit. Do not\n"
    "     pad to '[0001]' or strip to '1'.\n"
    "   - The number of paragraph IDs in the output MUST equal the number of\n"
    "     paragraph IDs in the source.\n"
    "2) Korean headings in corner brackets '【...】': translate the text, remove '【】' in output.\n"
    "   Examples: '【발명의 명칭】' → 'TITLE OF THE INVENTION', '【요약】' → 'ABSTRACT'.\n"
    "3) Curly braces '{...}': if content is already English, keep it as-is.\n"
    "   Example: '반도체 패키지{SEMICONDUCTOR PACKAGE}' → 'Semiconductor package {SEMICONDUCTOR PACKAGE}'.\n"
    "4) REFERENCE CHARACTERS AND NUMERALS — USPTO style (no brackets):\n"
    "   - Reference characters are alphanumeric labels that contain at least one digit\n"
    "     (e.g., 100, 100a, S10, WF1, R2, SR2, GR1, NF2). In USPTO English they appear\n"
    "     AFTER the noun phrase, WITHOUT brackets.\n"
    "   - Digit-only parentheses: remove brackets, keep the numeral.\n"
    "       '반도체 패키지(1000)' → 'a semiconductor package 1000'\n"
    "       '패키지 기판(100)'    → 'a package substrate 100'\n"
    "   - Alphanumeric reference characters with at least one digit: remove brackets,\n"
    "     keep the reference character after the noun phrase.\n"
    "       '전극(100a)'  → 'an electrode 100a'\n"
    "       '단계(S10)'   → 'step S10'\n"
    "       '구조체(WF1)' → 'structure WF1'\n"
    "   - Compact sub-index notation in the source — a reference prefix followed\n"
    "     immediately by a bracketed digit/alphanumeric (no space between them) —\n"
    "     is the SAME kind of reference character. Drop the inner brackets and\n"
    "     concatenate, then place it after the noun phrase:\n"
    "       '그루브(GR(1))'   → 'groove GR1'    (NOT 'groove GR(1)', NOT 'groove (GR1)')\n"
    "       '그루브들(GR(2))' → 'grooves GR2'\n"
    "       'NF2(1)'          → 'NF21'\n"
    "       'NF1(5)'          → 'NF15'\n"
    "       'NF2(2a)'         → 'NF22a'\n"
    "   - When the source lists several reference characters together —\n"
    "     '제1 및 제2 그루브들(GR(1), GR(2))' — translate to a natural English\n"
    "     listing of the normalized refs: 'first and second grooves GR1 and GR2'.\n"
    "     Never keep an outer bracket list around already-bracketed inner refs in\n"
    "     the output.\n"
    "   - Pure-letter reference characters are possible in Korean patent specs.\n"
    "     When an uppercase letter code in parentheses follows a Korean technical\n"
    "     component noun, treat it as a reference character: remove the parentheses\n"
    "     and place it after the noun phrase.\n"
    "       '발광 소자(LD)' → 'a light emitting device LD'\n"
    "       '게이트 컨택(GC)' → 'a gate contact GC'\n"
    "   - Do NOT remove parentheses around true abbreviations/acronyms, units, or\n"
    "     explanatory English names where the parenthetical is not a reference\n"
    "     character. Keep as-is: (CPO), (AI), (MSB), (LSB), (J), (W),\n"
    "     (Optic Engine Unit: OEU), (see FIG. 1).\n"
    "   - Pure math/variable symbols WITHOUT brackets are preserved exactly:\n"
    "     T1, BIT_3k, θ_k. Genuine equation notation that uses brackets together\n"
    "     with math operators or non-ASCII subscript brackets — e.g.\n"
    "     '〖BIT〗_(3k+1)' — is preserved exactly (it contains '+', which is not\n"
    "     part of a reference character).\n"
    "   - This rule does NOT apply to paragraph-ID brackets (rule 1) or to\n"
    "     [EQUATION] / [EQUATION_N] markers (rule from STYLE section).\n"
)

_STYLE_RULES = (
    "\n"
    "GLOBAL STYLE RULES (English patent drafting):\n"
    "- Use formal, clear, objective tone. Avoid contractions and casual wording.\n"
    "- Prefer 'comprising' when listing elements.\n"
    "- Use 'configured to', 'adapted to', 'at least one', 'plurality of' where appropriate.\n"
    "- Use passive constructions: 'may be disposed on', 'may include', 'may be formed of'.\n"
    "- Use standard patent phrasing: 'according to some embodiments', 'in the present embodiment'.\n"
    "- Always use 'FIG.' (uppercase) for figure references. Never use 'figure'.\n"
    "  Examples: '도 1' → 'FIG. 1',  '도 5a' → 'FIG. 5A'.\n"
    "- Translate '상면' as 'upper surface' and '하면' as 'lower surface' consistently.\n"
    "- Keep named entities, reference numerals, symbols, units, and formulas exactly as-is.\n"
    "- Preserve abbreviations/acronyms exactly. If the Korean introduces an English acronym\n"
    "  in parentheses, keep it in parentheses unless it is an alphanumeric reference character\n"
    "  with digits attached to a component/step noun.\n"
    "- If the input contains [EQUATION], keep that marker exactly where the equation belongs;\n"
    "  numbered forms such as [EQUATION_1], [EQUATION_2] are the same kind of marker and must\n"
    "  be preserved exactly, in the same order and relative position as the source;\n"
    "  it represents a preserved Word equation — do NOT translate the marker itself,\n"
    "  do NOT expand it into formula symbols, and do NOT replace it with a textual description.\n"
    "- Text AROUND an [EQUATION] marker (introductions like '다음 식을 만족하는', and parameter\n"
    "  legends such as '여기서, A는 ...,', 'B는 ...,', 'C는 ...') is REGULAR claim/specification text\n"
    "  and MUST be translated in full. Translate every parameter description — never drop, merge,\n"
    "  shorten, or summarize them, even when they are listed one item per line.\n"
    "  Map '여기서' to 'where' (or 'wherein' inside a claim) and translate every '<symbol>는/은 ...'\n"
    "  item as its own clause: 'A is ...', 'B is ...', 'C is ...'. Keep the same number of items\n"
    "  as the source — if the source lists 5 parameters, the translation lists 5 parameters.\n"
    "- Output ONLY the translated text — no commentary, explanations, or notes.\n"
)

_USPTO_SUBMISSION_RULES = (
    "\n"
    "USPTO SUBMISSION-DRAFTING CONSTRAINTS (STRICT):\n"
    "- Draft as filing-ready English patent application text, not as a literal\n"
    "  explanatory translation. The result must be legally faithful to the Korean\n"
    "  source while using conventional U.S. patent drafting grammar.\n"
    "- Do NOT add new technical matter, examples, advantages, alternatives,\n"
    "  conclusions, ranges, or definitions that are not in the Korean source.\n"
    "- Do NOT omit limitations, qualifiers, negative limitations, order of steps,\n"
    "  dependencies, reference numerals, or parameter definitions.\n"
    "- Avoid indefinite or vague wording unless the Korean source itself is vague:\n"
    "  avoid 'etc.', 'and so on', 'various', 'somehow', 'suitable', 'proper',\n"
    "  'thing', 'part', and unsupported 'preferably'.\n"
    "- Preserve modal force: Korean possibility/optionality maps to 'may'; required\n"
    "  or defining limitations map to present tense or 'is/are configured to'.\n"
    "  Do not convert optional embodiments into mandatory claim limitations, and\n"
    "  do not weaken mandatory claim limitations into optional language.\n"
    "- Preserve singular/plural and open-ended scope where possible. Use 'one or\n"
    "  more' only when the source supports it; otherwise use normal antecedent-\n"
    "  basis articles ('a/an' first, 'the' later).\n"
    "- Use 'embodiment' language in the specification, but do not import\n"
    "  embodiment-only features into the claims.\n"
    "- Use U.S. patent terms consistently: 'comprising', 'configured to',\n"
    "  'disposed on', 'coupled to', 'formed on', 'at least one', 'plurality of',\n"
    "  'wherein', and 'FIG.' where appropriate.\n"
    "- For inanimate technical relationships, prefer 'of' constructions over\n"
    "  apostrophe possessives, e.g., 'a surface of the substrate' rather than\n"
    "  'the substrate's surface', unless an apostrophe form is part of a name.\n"
    "- Avoid prosecution-risk phrasing in claims: no 'invention is', no intended\n"
    "  result standing alone as a limitation, no marketing language, no unsupported\n"
    "  relative terms such as 'excellent', 'improved', or 'high-performance'.\n"
)

_HEADING_RULES = (
    "\n"
    "HEADING NORMALIZATION:\n"
    "- '【요약】' or '요약서' → 'ABSTRACT'\n"
    "- '【청구 범위】' or '【청구범위】' → 'CLAIMS'\n"
    "- Remove '【】' characters; output heading as plain text.\n"
)


# ---------------------------------------------------------------------------
# Antecedent basis — strict USPTO article usage. Korean has no articles, so
# every noun phrase in the English output needs an article decided on a
# *mechanical* rule: 'a/an' on first mention, 'the' on every later mention of
# the same noun. This is one of the most common drafting errors in machine-
# translated Korean patents and is worth its own rule block with concrete
# BAD/GOOD examples.
# ---------------------------------------------------------------------------
_ANTECEDENT_RULES = (
    "\n"
    "ANTECEDENT BASIS — MECHANICAL ARTICLE USAGE (CRITICAL):\n"
    "English patents use articles by a STRICT rule, not by stylistic judgment:\n"
    "- FIRST mention of any countable noun phrase in this document: introduce\n"
    "  it with 'a' or 'an'. This is how the noun gains antecedent basis.\n"
    "- EVERY SUBSEQUENT mention of THAT SAME noun (or one that is clearly the\n"
    "  same instance): refer back with 'the'.\n"
    "- The choice 'a/an' vs 'the' is MECHANICAL: it depends only on whether the\n"
    "  noun has appeared earlier, not on emphasis or style.\n"
    "- The ESTABLISHED TERMINOLOGY block below lists Korean → English terms that\n"
    "  already appeared in earlier chunks. Treat each of those as ALREADY\n"
    "  INTRODUCED: use 'the' (or possessives like 'its', 'their') on every\n"
    "  occurrence in this chunk, NEVER 'a/an'.\n"
    "- A noun NOT in that block, appearing for the first time, takes 'a/an'\n"
    "  on its FIRST mention in this chunk, then 'the' on every later mention\n"
    "  within the same chunk.\n"
    "- Plural and mass nouns: use 'the' on subsequent mention; bare plurals/\n"
    "  mass nouns are acceptable on first mention when the source is generic\n"
    "  ('layers may include …').\n"
    "\n"
    "Examples:\n"
    "  BAD : 'The semiconductor device includes the substrate.'\n"
    "        (no antecedent for 'the substrate'; first mention should be 'a')\n"
    "  BAD : 'The device includes a substrate. A substrate is silicon.'\n"
    "        (second mention should be 'the', not 'a')\n"
    "  BAD : 'A substrate may be silicon. A first layer is on a substrate.'\n"
    "        ('substrate' mentioned twice — second 'a' should be 'the')\n"
    "  GOOD: 'The device includes a substrate, a first layer, and a contact.\n"
    "         The substrate may be silicon. The first layer is disposed on\n"
    "         the substrate.'\n"
    "  GOOD (term in ESTABLISHED TERMINOLOGY):\n"
    "        'The semiconductor device further includes a second contact on\n"
    "         the semiconductor device's upper surface.'  ← 'the' because\n"
    "         the term has already been introduced in an earlier chunk.\n"
)

COMMON_SYSTEM = (
    "You are a professional patent translation engine (Korean → English).\n"
    "Your output will be used in an English patent application.\n"
    "Follow all instructions exactly. Keep meaning legally faithful;\n"
    "do not invent or omit technical details.\n"
    "\n"
    "TERMINOLOGY CONSISTENCY:\n"
    "- Use the same English term for the same Korean term throughout.\n"
    "- If a term appeared in the conversation history, reuse that translation.\n"
    + _BRACKET_RULES
    + _HEADING_RULES
    + _STYLE_RULES
    + _USPTO_SUBMISSION_RULES
    + _ANTECEDENT_RULES
)

@dataclass(frozen=True)
class Prompt:
    name: str
    system: str


# ---------------------------------------------------------------------------
# Section-specific prompts
# ---------------------------------------------------------------------------

PROMPT_BODY = Prompt(
    name="body",
    system=(
        COMMON_SYSTEM
        + "\n"
        "SECTION: DESCRIPTION / DETAILED DESCRIPTION / BACKGROUND / DRAWINGS\n"
        "- Keep technical explanations precise and complete.\n"
        "- Use consistent noun phrases for components (e.g., 'a substrate', 'a passivation layer').\n"
        "- Preserve all reference numerals/characters but render them without brackets\n"
        "  in USPTO style: 100, 100a, S10, GR1 (from source 'GR(1)'), NF21 (from 'NF2(1)'),\n"
        "  T1. Keep pure math symbols (θ_k, BIT_3k) exactly.\n"
        "- Do not add advantages or conclusions unless explicitly stated in the source.\n"
        "- Maintain support for every claim limitation: translate structural relationships,\n"
        "  process order, materials, signal/data names, and conditional language completely.\n"
        "- Use specification style for embodiments: 'according to an embodiment',\n"
        "  'in some embodiments', 'may', 'can', and passive construction where supported.\n"
        "- Do not make the specification narrower than the Korean source by changing\n"
        "  optional embodiment wording into mandatory wording.\n"
        "\n"
        "EQUATIONS INSIDE A BODY PARAGRAPH:\n"
        "- Each equation is shown as a numbered marker [EQUATION_1], [EQUATION_2], … .\n"
        "  Keep every marker verbatim and in the SAME order and relative position as the source.\n"
        "- DO NOT regroup the markers (e.g. '[EQUATION_1] [EQUATION_2] description1 description2' is WRONG).\n"
        "  Output must alternate marker + its description, marker + its description, just like the source:\n"
        "    'where the variable satisfies [EQUATION_1], in which A is …, B is … . The system also satisfies\n"
        "     [EQUATION_2], in which X is …, Y is … .'\n"
        "- A parameter legend immediately following an equation ('여기서, A는 ..., B는 ...,') describes\n"
        "  THAT equation only — keep it adjacent to the same marker. Translate every '<symbol>는/은 ...'\n"
        "  item; do not merge, drop, or summarize.\n"
        "  BAD : 'α, β, and γ are X, Y, and Z, respectively.'\n"
        "  BAD : 'wherein is a phase difference value.'\n"
        "  GOOD: 'α is X; β is Y; γ is Z.'\n"
        "- The number of [EQUATION_N] tokens you emit MUST equal the number you received.\n"
    ),
)

PROMPT_ABSTRACT = Prompt(
    name="abstract",
    system=(
        COMMON_SYSTEM
        + "\n"
        "SECTION: ABSTRACT\n"
        "- Keep the translation concise, typically a single paragraph.\n"
        "- Focus on: what the invention is, key components, and core operation.\n"
        "- Do not include legal arguments, advantages, or marketing language.\n"
        "- Do not include claim-style numbering, bullet lists, reference-character-only\n"
        "  descriptions, citations to claims, or commentary.\n"
        "- Do not broaden or narrow the invention beyond the Korean abstract.\n"
    ),
)

_CLAIMS_BASE_RULES = (
    "\n"
    "SECTION: CLAIMS\n"
    "\n"
    "FORBIDDEN PHRASES (ABSOLUTE BAN — never use any of these):\n"
    "  * 'according to claim'\n"
    "  * 'as claimed in claim'\n"
    "  * 'pursuant to claim'\n"
    "  * 'in accordance with claim'\n"
    "  The ONLY allowed reference style is: 'The <noun phrase> of claim <N>, ...'\n"
    "\n"
    "'COMPRISING' DISCIPLINE (STRICT — USPTO open-ended transition):\n"
    "- Independent claim transition: 'comprising:' (NEVER 'including:',\n"
    "  'containing:', 'consisting of:' unless explicitly required by the source).\n"
    "- Dependent claim that adds a NEW element/step: 'further comprising'\n"
    "  (followed by the element noun phrase or gerund step). NEVER use any of\n"
    "  these as the dependent-claim transitional phrase:\n"
    "    BAD : '..., further including a layer'\n"
    "    BAD : '..., further includes a layer'\n"
    "    BAD : '..., further included a layer'\n"
    "    BAD : '..., additionally including a layer'\n"
    "    BAD : '..., further containing a layer'\n"
    "    GOOD: '..., further comprising a layer'\n"
    "    GOOD: '..., further comprising forming a layer on the substrate'\n"
    "- Korean cue: '~을/를 더 포함하는' / '단계를 더 포함하는' / '~을 추가로 포함하는'\n"
    "  → always 'further comprising ...'\n"
    "\n"
    "EQUATIONS INSIDE A CLAIM:\n"
    "- [EQUATION] markers, including numbered markers like [EQUATION_1], stand in for\n"
    "  Word equations; keep each marker exactly where it appears when present.\n"
    "- Detailed equation-layout and parameter-legend rules are supplied only for claim\n"
    "  chunks that actually contain equation markers.\n"
    "\n"
    "FORMATTING:\n"
    "- After ':' and after each ';', insert a newline to list elements on separate lines.\n"
    "- Do NOT capitalize the first word after ':', ';', or 'wherein' unless it is a proper noun.\n"
    "  Correct:   '... comprising:\\na first die;\\na second die.'\n"
    "  Incorrect: '... comprising:\\nA first die;\\nA second die.'\n"
    "- 'wherein' introduces a limitation — the word after 'wherein' must be lowercase.\n"
    "  Correct:   ', wherein the first groove has a first depth'\n"
    "  Incorrect: ', wherein The first groove has a first depth'\n"
    "- Maintain antecedent basis: introduce with 'a/an', refer back with 'the'.\n"
    "- Use 'wherein' (not 'where') for limitations.\n"
    "- Keep each claim as one sentence.\n"
    "- Preserve claim numbers and dependency references exactly.\n"
    "- Do NOT add or remove limitations.\n"
    "- Translate ALL content inside Korean parentheses/brackets in the claim.\n"
    "  Parenthetical/bracketed text may be a qualifier, alternative, signal\n"
    "  name, reference character, range, or definition; never silently drop it.\n"
    "- Do NOT use reference numerals as substitutes for claim elements. If a\n"
    "  reference numeral is present, keep it after the noun phrase, e.g.,\n"
    "  'a substrate 100', not just '100'.\n"
    "- Avoid means-plus-function phrasing ('means for') unless the Korean source\n"
    "  explicitly requires it.\n"
    "- Functional language must be tied to structure when the Korean source does so:\n"
    "  use 'configured to <verb>' or a structural clause, not a bare intended result.\n"
    "- Do not introduce 'the' before a claim element unless that exact element has\n"
    "  already been introduced in the same claim or in the locked dependent preamble.\n"
)


# ---------------------------------------------------------------------------
# Per-kind claim sub-prompts.
# Each appends a kind-specific block to _CLAIMS_BASE_RULES. The differences
# are: independent preamble template, element grammar (noun phrase vs gerund
# vs instructions), and the dependent connective ('wherein' vs
# 'further comprising').
# ---------------------------------------------------------------------------

_CLAIMS_DEVICE_EXTRA = (
    "\n"
    "CLAIM KIND: DEVICE\n"
    "INDEPENDENT PREAMBLE: 'A <noun phrase> comprising:'.\n"
    "  The <noun phrase> MUST come from the trailing Korean noun in the\n"
    "  claim — '…을 포함하는 X' where X is the subject of the claim. Translate X\n"
    "  into idiomatic English (don't summarize away the qualifier):\n"
    "    '반도체 장치'         → 'semiconductor device'\n"
    "    '메모리 장치'         → 'memory device'\n"
    "    '발광 다이오드 패키지' → 'light-emitting diode package'\n"
    "    '디바이스'           → 'device'\n"
    "    '회로'               → 'circuit'\n"
    "    '모듈'               → 'module'\n"
    "    '어셈블리'           → 'assembly'\n"
    "    '기판'               → 'substrate'\n"
    "Do not use the generic unqualified word 'apparatus' when the Korean source\n"
    "  says only '장치'; use 'device' in that case. A qualified technical term\n"
    "  such as 'LiDAR apparatus' is allowed when the source includes that\n"
    "  qualifier (e.g., '라이다 장치').\n"
    "ELEMENT GRAMMAR: each element is a NOUN PHRASE introduced with 'a/an',\n"
    "  e.g., 'a first die;', 'a substrate;', 'a gate electrode disposed on the substrate;'.\n"
    "  Do NOT use gerund (-ing) verb forms — those are for method claims only.\n"
    "DEPENDENT PREAMBLE: 'The <noun phrase> of claim <N>, wherein ...'.\n"
    "  Use 'wherein' to introduce limitations on existing elements.\n"
)

_CLAIMS_METHOD_EXTRA = (
    "\n"
    "CLAIM KIND: METHOD / PROCESS\n"
    "INDEPENDENT PREAMBLE:\n"
    "  - 'A method comprising:' (when no object is specified), or\n"
    "  - 'A method of <gerund object>, the method comprising:'\n"
    "    (e.g., 'A method of manufacturing a semiconductor device, the method comprising:').\n"
    "ELEMENT GRAMMAR: each step is a GERUND ('-ing' verb form), not a noun phrase:\n"
    "  e.g., 'forming a first layer on a substrate;', 'etching a portion of the first layer;'.\n"
    "  Korean steps end with the verb ('~하는 단계;'). Invert so the verb (gerund) comes first\n"
    "  and the objects/locations follow, in English order.\n"
    "DEPENDENT PREAMBLE — pick the right connective:\n"
    "  - 'The method of claim <N>, wherein ...'  → when refining an existing step\n"
    "    (Korean: '상기 ~ 단계는, ...').\n"
    "  - 'The method of claim <N>, further comprising <gerund> ...' → when adding a NEW step\n"
    "    (Korean: '~ 단계를 더 포함하는' / '더 포함하는 ~ 단계').\n"
    "  Use 'further comprising' ONLY for added steps. Use 'wherein' to qualify existing steps.\n"
)

_CLAIMS_CRM_EXTRA = (
    "\n"
    "CLAIM KIND: NON-TRANSITORY COMPUTER-READABLE MEDIUM (CRM)\n"
    "INDEPENDENT PREAMBLE:\n"
    "  'A non-transitory computer-readable medium storing instructions that, when executed by\n"
    "   <actor>, cause the <actor> to:'\n"
    "  where <actor> is typically 'a processor', 'a system', 'one or more processors', etc.,\n"
    "  taken from the source. Reuse the SAME actor noun phrase in every dependent.\n"
    "ELEMENT GRAMMAR: each step is a bare-infinitive verb, e.g., 'receive a request;',\n"
    "  'process the request;', 'transmit the result;'. Each step is a thing the actor does.\n"
    "DEPENDENT PREAMBLE:\n"
    "  - 'The non-transitory computer-readable medium of claim <N>, wherein ...' (refines).\n"
    "  - 'The non-transitory computer-readable medium of claim <N>, wherein the instructions\n"
    "     further cause the <actor> to <bare-infinitive> ...' (adds a new instruction step).\n"
)

_CLAIMS_SYSTEM_EXTRA = (
    "\n"
    "CLAIM KIND: SYSTEM\n"
    "INDEPENDENT PREAMBLE:\n"
    "  - Plain: 'A system comprising:' followed by structural elements (device-style).\n"
    "  - Processor + memory pattern (very common):\n"
    "    'A system comprising:\\n"
    "       a processor; and\\n"
    "       a memory storing instructions that, when executed by the processor, cause the\n"
    "       processor to:\\n"
    "         <bare-infinitive step>;\\n"
    "         <bare-infinitive step>; ...'\n"
    "  Reuse the SAME actor noun phrase ('the processor', 'the system') in every dependent.\n"
    "ELEMENT GRAMMAR: structural elements are noun phrases; instruction steps are\n"
    "  bare-infinitive verbs. Korean '~하도록 구성된' → 'configured to <bare-infinitive>'.\n"
    "DEPENDENT PREAMBLE: 'The system of claim <N>, wherein ...'.\n"
)


PROMPT_CLAIMS_DEVICE = Prompt(
    name="claims_device",
    system=COMMON_SYSTEM + _CLAIMS_BASE_RULES + _CLAIMS_DEVICE_EXTRA,
)
PROMPT_CLAIMS_METHOD = Prompt(
    name="claims_method",
    system=COMMON_SYSTEM + _CLAIMS_BASE_RULES + _CLAIMS_METHOD_EXTRA,
)
PROMPT_CLAIMS_CRM = Prompt(
    name="claims_crm",
    system=COMMON_SYSTEM + _CLAIMS_BASE_RULES + _CLAIMS_CRM_EXTRA,
)
PROMPT_CLAIMS_SYSTEM = Prompt(
    name="claims_system",
    system=COMMON_SYSTEM + _CLAIMS_BASE_RULES + _CLAIMS_SYSTEM_EXTRA,
)


CLAIM_PROMPT_BY_KIND: dict[str, Prompt] = {
    "device": PROMPT_CLAIMS_DEVICE,
    "method": PROMPT_CLAIMS_METHOD,
    "crm":    PROMPT_CLAIMS_CRM,
    "system": PROMPT_CLAIMS_SYSTEM,
}


from .claim_classifier import (
    ClaimKind,
    MultiParent,
    PreambleSpec,
    build_dependent_preamble,
    format_parent_reference,
)
from .glossary import format_for_prompt


_EQUATION_TOKEN_RE = re.compile(r'\[EQUATION_\d+\]')
_HANGUL_RUN_RE = re.compile(r'[가-힯]{3,}')


_PREAMBLE_PLANNER_SYSTEM = (
    "You are a Korean-to-English patent claim preamble planner.\n"
    "Do NOT translate the full claim. Identify the claim subject and produce\n"
    "only the English preamble information needed for a USPTO-style claim.\n"
    "Preserve technical qualifiers in the claim subject; do not replace a\n"
    "specific subject with a generic word unless the Korean is itself generic.\n"
    "Choose filing-ready USPTO preamble language. Do not use 'apparatus' as a\n"
    "generic substitute for a concrete Korean subject such as device, circuit,\n"
    "module, package, substrate, system, method, or computer-readable medium.\n"
    "Respond with valid JSON only.\n"
)


def _has_combined_legend_pattern(chunk_text: str) -> bool:
    """True when the source has N>=2 equation markers grouped together
    (no Korean text between them) followed by a single combined parameter
    legend ('여기서, A는 …, B는 …, X는 …').

    Example pattern (from a claim):
        다음 식들을 만족한다:
        [EQUATION_1]
        [EQUATION_2]
        [EQUATION_3]
        여기서, A는 ..., B는 ..., X는 ..., Y는 ..., M은 ..., N은 ...

    Without intervention the LLM faithfully copies this layout — equations
    grouped, then one big legend. Detect it and tell the model to split the
    legend so each equation is followed only by the parameters it actually
    contains.
    """
    matches = list(_EQUATION_TOKEN_RE.finditer(chunk_text))
    if len(matches) < 2:
        return False
    for a, b in zip(matches, matches[1:]):
        between = chunk_text[a.end():b.start()]
        if _HANGUL_RUN_RE.search(between):
            return False  # already separated by Korean text — not combined
    after_last = chunk_text[matches[-1].end():]
    return bool(_HANGUL_RUN_RE.search(after_last))


_COMBINED_LEGEND_INSTRUCTION = (
    "\n"
    "PARAMETER LEGEND FORMAT — ABSOLUTELY STRICT:\n"
    "The Korean source has a parameter legend ('여기서, …' / '상기 …에서, …')\n"
    "that defines the variables used in the equation(s). When you translate it:\n"
    "\n"
    "1. Each parameter gets its OWN clause that begins with its symbol followed\n"
    "   immediately by 'is' (or 'denotes'). Clauses are joined by ';'.\n"
    "2. NEVER list symbols separately from their descriptions. NEVER use\n"
    "   'respectively' form. NEVER use comma-list form.\n"
    "3. Preserve symbols EXACTLY as written in the source — including\n"
    "   subscripts (θ_k), CJK math brackets (〖BIT〗_3k, 〖BIT〗_(3k+1)),\n"
    "   parentheses, primes, and Greek letters. Do not strip or simplify them.\n"
    "4. The output clause count MUST equal the source clause count\n"
    "   (one '<symbol>는/은 …' item ⇒ one English clause). Do not merge or drop.\n"
    "\n"
    "BAD vs GOOD (study these — your output must match GOOD):\n"
    "  BAD : α, β, γ are X, Y, Z, respectively\n"
    "  BAD : α, β, and γ denote X, Y, and Z\n"
    "  BAD : LLR θ_k 〖BIT〗_3k 〖BIT〗_(3k+1) μ are X, Y, Z, W, V\n"
    "  BAD : wherein\\nLLR θ_k 〖BIT〗_3k μ\\nis A; is B; is C; is D\n"
    "  BAD : LLR is A; θ_k 〖BIT〗_3k μ are B, C, D\n"
    "  GOOD: α is X; β is Y; γ is Z\n"
    "  GOOD: wherein LLR is the bit reliability data;\n"
    "        θ_k is the phase-difference value of the k-th phase-difference data;\n"
    "        〖BIT〗_3k is the …;\n"
    "        〖BIT〗_(3k+1) is the …;\n"
    "        μ is the noise factor\n"
    "\n"
    "GROUPED-EQUATION LAYOUT (when the source has multiple [EQUATION_N] markers\n"
    "grouped consecutively with one combined legend afterward):\n"
    "- SPLIT the legend so each [EQUATION_N] is followed by ONLY the clauses\n"
    "  whose symbols actually appear in that equation:\n"
    "    [EQUATION_1], where <s1> is <m1>; <s2> is <m2>;\n"
    "    [EQUATION_2], where <s3> is <m3>; <s4> is <m4>;\n"
    "    [EQUATION_3], where <s5> is <m5>; <s6> is <m6>.\n"
    "- If the Korean source itself uses '각각' / '순차적으로' (respectively /\n"
    "  in order), STILL expand into per-parameter clauses in English.\n"
)


def _system_with_glossary(prompt: Prompt, glossary: dict[str, str]) -> str:
    """Inject the rolling glossary into the system message.

    The glossary is a terminology ledger only. It must not be used as an
    antecedent-basis ledger because claims are translated before the body and
    each independent claim must establish its own antecedent basis.
    """
    block = format_for_prompt(glossary)
    return prompt.system + (
        "\n"
        "ESTABLISHED TERMINOLOGY (Korean → English).\n"
        "Use this list ONLY for consistent Korean-to-English term choice:\n"
        "  - Reuse the EXACT English wording shown for the same Korean term.\n"
        "  - Do NOT treat this list as antecedent basis. A glossary term is not\n"
        "    automatically 'already introduced' in the current paragraph or claim.\n"
        "  - Choose 'a/an' vs 'the' from the current claim/paragraph context:\n"
        "    first mention in that context takes 'a/an' where appropriate;\n"
        "    later references to the same introduced element take 'the'.\n"
        f"{block}\n"
    )


def _format_equation_context(equation_context: dict[str, str] | None) -> str:
    if not equation_context:
        return ""
    lines = [
        "",
        "EQUATION FORMULA CONTEXT (DO NOT output this block):",
        "- Use these formulas only to pair each Korean parameter legend item",
        "  with the equation and symbol it describes.",
        "- Keep only the [EQUATION_N] markers in the translation. Do not copy,",
        "  restate, translate, or expand these formula-context lines.",
    ]
    for marker, formula in equation_context.items():
        lines.append(f"  {marker}: {formula}")
    return "\n".join(lines) + "\n"


def build_preamble_plan_messages(
    *,
    claim_num: int,
    chunk_text: str,
    kind: ClaimKind,
    glossary: dict[str, str],
) -> list[dict]:
    glossary_block = format_for_prompt(glossary)
    user = (
        f"Plan the English preamble for Korean independent claim {claim_num}.\n"
        f"Detected claim kind: {kind}\n"
        "\n"
        "Return JSON ONLY in this schema:\n"
        "{"
        '"korean_subject_span": "<Korean words that name the claimed subject>", '
        '"english_noun_phrase": "<English noun phrase for dependent preambles>", '
        '"independent_preamble": "<exact English independent-claim opening>", '
        '"actor_phrase": "<actor phrase for CRM/system, or empty string>", '
        '"confidence": "high|medium|low"'
        "}\n"
        "\n"
        "Rules:\n"
        "- For device/circuit/module/system-style claims, the independent preamble usually starts\n"
        "  'A <english_noun_phrase> comprising:'.\n"
        "- For method claims, use either 'A method comprising:' or\n"
        "  'A method of <gerund object>, the method comprising:'. The dependent noun phrase is 'method'.\n"
        "- For non-transitory computer-readable medium claims, use the standard CRM preamble and\n"
        "  identify the actor phrase, e.g. 'processor' or 'one or more processors'.\n"
        "- Do not use 'apparatus' unless the Korean subject specifically requires it.\n"
        "- Keep the noun phrase specific but not overloaded: include the claim subject\n"
        "  and its essential qualifier, but do not import body limitations into the preamble.\n"
        "- The independent_preamble must end with ':' when it introduces claim elements.\n"
        "- Do not add limitations from the body of the claim into the noun phrase.\n"
        "- Reuse glossary terms where applicable.\n"
        "\n"
        f"Glossary:\n{glossary_block}\n"
        "\n"
        "Korean claim:\n"
        f"{chunk_text}\n"
    )
    return [
        {"role": "system", "content": _PREAMBLE_PLANNER_SYSTEM},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Body — translate one chunk per call (chunk may span multiple paragraphs joined with \n)
# ---------------------------------------------------------------------------

def build_body_messages(
    chunk_text: str,
    glossary: dict[str, str],
    equation_context: dict[str, str] | None = None,
) -> list[dict]:
    system = _system_with_glossary(PROMPT_BODY, glossary)
    layout_repair = (
        _COMBINED_LEGEND_INSTRUCTION
        if _has_combined_legend_pattern(chunk_text) else ""
    )
    user = (
        "Translate the following Korean patent text into English (USPTO style).\n"
        "Render the entire text as ONE coherent English paragraph.\n"
        "- If the source contains [EQUATION_1], [EQUATION_2], … keep each marker verbatim\n"
        "  and in the SAME order and relative position as the source. Do not regroup them.\n"
        "- If the source alternates marker + description, marker + description, the translation\n"
        "  must alternate the SAME way: e.g. '[EQUATION_1] + its description, then [EQUATION_2] +\n"
        "  its description.' NEVER output '[EQUATION_1] [EQUATION_2] description1 description2'.\n"
        "- When translating '여기서' parameter legends, keep the symbol attached to its own\n"
        "  description. NEVER write a bare clause like 'wherein is ...' with the symbol omitted.\n"
        "- Emit exactly the same number of [EQUATION_N] tokens as the input.\n"
        f"{layout_repair}"
        f"{_format_equation_context(equation_context)}"
        "Output ONLY the English translation. Do not output JSON, markdown, commentary, or notes.\n"
        "\n"
        "Korean:\n"
        f"{chunk_text}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_body_retry_messages(
    *,
    previous_messages: list[dict],
    problem: str,
    chunk_text: str,
) -> list[dict]:
    """Strengthen a body prompt after an invalid/untranslated response."""
    retry = (
        "The previous response was invalid and must be corrected.\n"
        f"Problem: {problem}\n"
        "\n"
        "Translate the Korean patent paragraph completely into English now.\n"
        "- Do not leave any Korean/Hangul text in the English paragraph.\n"
        "- Do not output JSON, schema text, commentary, or markdown.\n"
        "- If the source paragraph originally had a numeric paragraph ID such as [001], "
        "preserve it only if it appears in the text shown below.\n"
        "- Output ONLY the complete English paragraph.\n"
        "\n"
        "Korean paragraph to translate:\n"
        f"{chunk_text}\n"
    )
    return previous_messages + [{"role": "user", "content": retry}]


def build_simple_body_messages(chunk_text: str, glossary: dict[str, str]) -> list[dict]:
    glossary_block = format_for_prompt(glossary)
    return [
        {
            "role": "system",
            "content": (
                "Translate Korean patent specification text into filing-ready "
                "USPTO-style English. Output plain English text only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Translate this Korean patent specification text into USPTO style.\n"
                "Rules: preserve reference numerals, symbols, paragraph IDs, FIG. references, "
                "and [EQUATION_N] markers exactly; do not use markdown or JSON; do not leave Korean.\n"
                f"Use these claim-derived terms consistently when applicable:\n{glossary_block}\n\n"
                f"Korean:\n{chunk_text}\n"
            ),
        },
    ]


def build_body_glossary_messages(
    korean_text: str,
    english_text: str,
    glossary: dict[str, str],
) -> list[dict]:
    """Extract/update glossary terms after a body/description translation."""
    glossary_block = format_for_prompt(glossary)
    system = (
        "You extract Korean-to-English technical terminology from patent "
        "description text.\n"
        "Return valid JSON only."
    )
    user = (
        "Update the glossary using this translated description text.\n"
        "The glossary was seeded from the claims, so claim terminology has priority.\n"
        "Rules:\n"
        "- If a Korean term already exists in the glossary, keep its English term unchanged.\n"
        "- Add only significant technical noun phrases: components, materials, signals,\n"
        "  data structures, operations, measured properties, equation parameters, and named modules.\n"
        "- Skip generic patent/legal words, articles, verbs, whole sentences, reference numerals alone,\n"
        "  and generic words such as embodiment, invention, claim, method, device, comprising, wherein.\n"
        "- Do not use this task to revise the translation. Extract terms only.\n"
        "\n"
        f"Existing glossary:\n{glossary_block}\n"
        "\n"
        "Return JSON only in this format:\n"
        '[{"ko": "<Korean term>", "en": "<English term>"}, ...]\n'
        "\n"
        f"Korean description text:\n{korean_text}\n"
        "\n"
        f"English description text:\n{english_text}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Sentence-level builders (used by translate_body's equation path)
# ---------------------------------------------------------------------------
# These translate ONE Korean text segment or ONE Korean parameter clause at
# a time so equation positions and per-symbol structure are preserved by
# construction rather than by a post-hoc redistribution heuristic.

def build_segment_messages(
    korean_segment: str,
    glossary: dict[str, str],
) -> list[dict]:
    """Translate a single Korean text segment between [EQUATION_N] markers."""
    system = _system_with_glossary(PROMPT_BODY, glossary)
    user = (
        "Translate this Korean patent text fragment into English (USPTO style).\n"
        "- Return ONE coherent English fragment, no leading/trailing punctuation\n"
        "  unless the source has it.\n"
        "- Do NOT add a paragraph ID, do NOT add commentary, do NOT add markdown.\n"
        "- Do NOT translate or output any [EQUATION_N] marker — the surrounding\n"
        "  code stitches markers in separately.\n"
        "- Output ONLY the English fragment. Do not output JSON.\n"
        "\n"
        "Korean:\n"
        f"{korean_segment}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_clause_messages(
    symbol: str,
    korean_clause: str,
    glossary: dict[str, str],
) -> list[dict]:
    """Translate one Korean '<sym>는 <desc>' parameter clause.

    The output MUST be exactly one clause beginning with ``symbol`` followed by
    'is' / 'denotes'. The caller stitches multiple clauses together with ';'.
    """
    system = _system_with_glossary(PROMPT_BODY, glossary)
    user = (
        "Translate this Korean parameter clause into ONE English clause in patent style.\n"
        f"Symbol: '{symbol}' — keep it VERBATIM as the first token of your output.\n"
        "- Required output shape: '<symbol> is <description>'  (or '<symbol> denotes <description>').\n"
        "- Do NOT include 'where', 'wherein', 'in which', semicolons, periods, or\n"
        "  surrounding punctuation. Just the clause.\n"
        "- Do NOT change, pad, or strip the symbol — including any brackets, subscripts,\n"
        "  or Unicode it contains (e.g. '〖BIT〗_3k', 'θ_k', 'α').\n"
        "- Output ONLY the English clause. Do not output JSON.\n"
        "\n"
        "Korean clause:\n"
        f"{korean_clause}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Abstract — single coherent paragraph
# ---------------------------------------------------------------------------

def build_abstract_messages(chunk_text: str, glossary: dict[str, str]) -> list[dict]:
    system = _system_with_glossary(PROMPT_ABSTRACT, glossary)
    user = (
        "Translate the following Korean patent ABSTRACT into one concise English paragraph.\n"
        "Output ONLY the English abstract. Do not output JSON, markdown, commentary, or notes.\n"
        "\n"
        "Korean:\n"
        f"{chunk_text}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_simple_abstract_messages(chunk_text: str, glossary: dict[str, str]) -> list[dict]:
    glossary_block = format_for_prompt(glossary)
    return [
        {
            "role": "system",
            "content": (
                "Translate Korean patent abstracts into concise USPTO-style English. "
                "Output plain English text only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Translate this Korean patent abstract into one concise English paragraph.\n"
                "Do not output JSON, markdown, notes, or Korean text.\n"
                f"Use these terms consistently when applicable:\n{glossary_block}\n\n"
                f"Korean abstract:\n{chunk_text}\n"
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Claims — one claim per call, returned as one coherent sentence
# ---------------------------------------------------------------------------

_SHARED_CLAIM_RULES = (
    "- Render the FULL claim as a single sentence.\n"
    "- Place ';' between elements; insert a newline after each ';' and after each ':'.\n"
    "- Use lowercase after ':', ';', and 'wherein' (unless a proper noun).\n"
    "- Do NOT prepend the claim number — numbering is added separately.\n"
)


def _has_equation_context(chunk_text: str, equation_context: dict[str, str] | None) -> bool:
    return bool(equation_context) or bool(_EQUATION_TOKEN_RE.search(chunk_text))


def _has_parameter_legend(chunk_text: str) -> bool:
    return (
        "여기서" in chunk_text
        or "각각" in chunk_text
        or bool(re.search(r'[A-Za-zΑ-Ωα-ω][A-Za-z0-9Α-Ωα-ω₀-₉_\']{0,12}\s*(?:는|은)', chunk_text))
    )


def _claim_equation_rules(
    chunk_text: str,
    equation_context: dict[str, str] | None,
) -> str:
    if not _has_equation_context(chunk_text, equation_context):
        return ""
    rules = (
        "\n"
        "EQUATION RULES FOR THIS CLAIM:\n"
        "- If [EQUATION] or a numbered marker such as [EQUATION_1] appears, keep the marker\n"
        "  verbatim in the same source order and relative position.\n"
        "- Preserve equation layout as much as possible. If the source alternates equation +\n"
        "  description, equation + description, translate in that same alternating order;\n"
        "  do not move all equations before all descriptions.\n"
    )
    if _has_parameter_legend(chunk_text) or _has_combined_legend_pattern(chunk_text):
        rules += (
            "- Translate every parameter description that follows an equation (e.g. '여기서, A는 ...,\n"
            "  B는 ..., C는 ...'). Output one clause per parameter — do not merge, drop, or\n"
            "  summarize any of them.\n"
            "- Never render parameter legends in comma-list/respectively form. Use one symbol-description\n"
            "  clause per parameter: 'α is ...; β is ...; γ is ...'.\n"
            "- Every parameter clause must explicitly begin with the symbol it describes. NEVER write\n"
            "  malformed clauses like 'wherein is ...' or '<symbol1> <symbol2> <symbol3>, μ is ...'.\n"
        )
    return rules


def _independent_preamble_lock(independent_preamble: str | None) -> str:
    if not independent_preamble:
        return ""
    return (
        "\n"
        f"PREAMBLE LOCK — begin the English claim EXACTLY with: '{independent_preamble}'\n"
        "- This preamble was planned from the Korean claim subject. Do not substitute a generic\n"
        "  noun such as 'apparatus' and do not add limitations to the preamble noun phrase.\n"
    )


def _independent_user_prompt(
    claim_num: int,
    kind: ClaimKind,
    chunk_text: str,
    equation_context: dict[str, str] | None = None,
    independent_preamble: str | None = None,
) -> str:
    layout_repair = (
        _COMBINED_LEGEND_INSTRUCTION
        if _has_combined_legend_pattern(chunk_text) else ""
    )
    return (
        f"Translate Korean claim {claim_num} (INDEPENDENT, kind={kind}) into ONE coherent English claim sentence.\n"
        "Use the kind-specific PREAMBLE template and ELEMENT GRAMMAR from the system message.\n"
        + _SHARED_CLAIM_RULES
        + _independent_preamble_lock(independent_preamble)
        + _claim_equation_rules(chunk_text, equation_context)
        + layout_repair
        + _format_equation_context(equation_context)
        + "\n"
        "Output ONLY the English claim sentence. Do not output the claim number, JSON, markdown, commentary, or notes.\n"
        "\n"
        "Korean:\n"
        f"{chunk_text}\n"
    )


def _korean_adds_claim_element(text: str) -> bool:
    return bool(re.search(r'(?:단계[를을]?\s*)?더\s*포함|[를을]\s*더\s*포함|추가로\s*포함', text))


def _dependent_user_prompt(
    claim_num: int,
    kind: ClaimKind,
    chunk_text: str,
    parent_spec: PreambleSpec,
    parent_claim_nums: list[int],
    multi_parent_kind: MultiParent,
    method_connective: str,
    equation_context: dict[str, str] | None = None,
) -> str:
    preamble = build_dependent_preamble(
        parent_spec, parent_claim_nums, multi_parent_kind
    )
    if kind == "method":
        # 'further comprising' is followed by a gerund step (no comma); 'wherein'
        # is followed by a refining clause (comma + lowercase clause).
        if method_connective == "further comprising":
            opener = f"'{preamble}, further comprising <gerund step> ...'"
        else:
            opener = f"'{preamble}, wherein <refining clause about an existing step> ...'"
    elif kind == "crm":
        opener = (
            f"'{preamble}, wherein the instructions further cause "
            f"the {parent_spec.actor_phrase or 'processor'} to <bare-infinitive> ...' "
            f"OR '{preamble}, wherein <refining clause> ...'"
        )
    elif kind in {"device", "system"} and _korean_adds_claim_element(chunk_text):
        opener = f"'{preamble}, further comprising <new element noun phrase> ...'"
    else:  # device, system
        opener = f"'{preamble}, wherein <limitation> ...'"

    layout_repair = (
        _COMBINED_LEGEND_INSTRUCTION
        if _has_combined_legend_pattern(chunk_text) else ""
    )
    return (
        f"Translate Korean claim {claim_num} (DEPENDENT, kind={kind}, "
        f"depends on {format_parent_reference(parent_claim_nums, multi_parent_kind)}) "
        "into ONE coherent English claim sentence.\n"
        "\n"
        f"PREAMBLE LOCK — begin the English claim EXACTLY with: {opener}\n"
        f"  - The phrase '{preamble}' must appear verbatim — do NOT change the noun "
        "phrase, the claim number, or the dependency style.\n"
        "  - Do NOT use 'according to claim', 'as claimed in', 'pursuant to', "
        "or 'in accordance with'.\n"
        "\n"
        + _SHARED_CLAIM_RULES
        + _claim_equation_rules(chunk_text, equation_context)
        + layout_repair
        + _format_equation_context(equation_context)
        + "\n"
        "Output ONLY the English claim sentence. Do not output the claim number, JSON, markdown, commentary, or notes.\n"
        "\n"
        "Korean:\n"
        f"{chunk_text}\n"
    )


def build_claim_messages(
    *,
    claim_num: int,
    chunk_text: str,
    glossary: dict[str, str],
    kind: ClaimKind = "device",
    is_independent: bool = True,
    parent_spec: PreambleSpec | None = None,
    parent_claim_nums: list[int] | None = None,
    multi_parent_kind: MultiParent = "single",
    method_connective: str = "wherein",
    equation_context: dict[str, str] | None = None,
    independent_preamble: str | None = None,
) -> list[dict]:
    """Build messages for one claim translation call.

    Routes to the per-kind system prompt (device/method/crm/system) and, for
    dependents, injects a literal preamble prefix derived from the parent
    independent claim's locked-in noun phrase. This eliminates the entire
    class of preamble drift bugs (wrong noun phrase, 'according to claim',
    method dependents using 'wherein' when they should use 'further comprising').
    """
    prompt = CLAIM_PROMPT_BY_KIND.get(kind, PROMPT_CLAIMS_DEVICE)
    system = _system_with_glossary(prompt, glossary)

    if is_independent or parent_spec is None or not parent_claim_nums:
        user = _independent_user_prompt(
            claim_num, kind, chunk_text, equation_context, independent_preamble
        )
    else:
        user = _dependent_user_prompt(
            claim_num=claim_num,
            kind=kind,
            chunk_text=chunk_text,
            parent_spec=parent_spec,
            parent_claim_nums=parent_claim_nums,
            multi_parent_kind=multi_parent_kind,
            method_connective=method_connective,
            equation_context=equation_context,
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_claim_retry_messages(
    *,
    previous_messages: list[dict],
    problem: str,
    chunk_text: str,
) -> list[dict]:
    """Strengthen a claim prompt after an invalid/untranslated response."""
    retry = (
        "The previous response was invalid and must be corrected.\n"
        f"Problem: {problem}\n"
        "\n"
        "Translate the Korean claim completely into English now.\n"
        "- Do not leave any Korean/Hangul text in the English claim.\n"
        "- Do not output JSON, schema text, commentary, or markdown.\n"
        "- Translate every Korean phrase inside parentheses/brackets; do not omit bracketed claim content.\n"
        "- Output ONLY the complete English claim sentence.\n"
        "\n"
        "Korean claim to translate:\n"
        f"{chunk_text}\n"
    )
    return previous_messages + [{"role": "user", "content": retry}]


def build_simple_claim_messages(
    *,
    claim_num: int,
    chunk_text: str,
    glossary: dict[str, str],
    required_opening: str | None = None,
    independent_preamble: str | None = None,
) -> list[dict]:
    glossary_block = format_for_prompt(glossary)
    opening = required_opening or independent_preamble
    opening_rule = (
        f"\nThe English claim must begin exactly with: {opening}\n"
        if opening else ""
    )
    return [
        {
            "role": "system",
            "content": (
                "Translate Korean patent claims into strict USPTO claim style. "
                "Output one plain English claim sentence only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Translate Korean claim {claim_num} into USPTO style.\n"
                f"{opening_rule}"
                "Rules: preserve all limitations, dependencies, reference numerals, symbols, "
                "and [EQUATION_N] markers; use comprising/wherein/further comprising correctly; "
                "translate every phrase inside parentheses/brackets; do not output the claim number, "
                "JSON, markdown, notes, or Korean text.\n"
                f"Use these terms consistently when applicable:\n{glossary_block}\n\n"
                f"Korean claim:\n{chunk_text}\n"
            ),
        },
    ]


def build_claim_glossary_messages(
    korean_claim: str,
    english_claim: str,
    glossary: dict[str, str],
) -> list[dict]:
    """Extract bilingual terminology after claim translation.

    This is intentionally separate from translation so malformed JSON can only
    reduce glossary coverage; it cannot invalidate an otherwise good claim.
    """
    glossary_block = format_for_prompt(glossary)
    system = (
        "You extract Korean-to-English technical terminology from patent claims.\n"
        "Return valid JSON only."
    )
    user = (
        "Extract significant technical noun phrases from this translated claim.\n"
        "Prefer components, materials, signals, processes, data structures, and claim subjects.\n"
        "Skip generic legal words such as claim, method, comprising, wherein, and configured.\n"
        "Reuse existing English terms when the Korean term is already in the glossary.\n"
        "\n"
        f"Existing glossary:\n{glossary_block}\n"
        "\n"
        "Return JSON only in this format:\n"
        '[{"ko": "<Korean term>", "en": "<English term>"}, ...]\n'
        "\n"
        f"Korean claim:\n{korean_claim}\n"
        "\n"
        f"English claim:\n{english_claim}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Review — decide + revise per section
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM = (
    "You are a senior patent translation reviewer (Korean → English).\n"
    "Assess translated patent text for the following issues:\n"
    "1. Terminology consistency — same Korean term must map to same English term.\n"
    "2. Translation accuracy — no omissions, additions, or hallucinations.\n"
    "   For CLAIMS, compare every Korean limitation, listed element, step,\n"
    "   dependency phrase, bracketed/parenthetical content, modifier, range, and negative limitation against the\n"
    "   English claim. Any omitted limitation is a filing-critical defect.\n"
    "3. Claim structure — independent device/system claims start with 'A <noun phrase> comprising:';\n"
    "   independent method claims start with 'A method comprising:' or 'A method of ..., the method comprising:';\n"
    "   CRM claims use the standard 'A non-transitory computer-readable medium storing instructions...' preamble.\n"
    "   Dependent claims start with 'The <same noun phrase> of claim N, wherein' unless a method dependent adds\n"
    "   a new step, in which case it starts with 'The method of claim N, further comprising'. NEVER 'according to claim'.\n"
    "4. Figure references — must be 'FIG. N' (uppercase, period after FIG).\n"
    "5. Reference numerals and characters — all reference numerals/characters from the\n"
    "   Korean source must be preserved where legally meaningful, but rendered in USPTO\n"
    "   style WITHOUT brackets and placed after the noun phrase: '100', '100a', 'S10',\n"
    "   'GR1' (from source 'GR(1)'), 'NF21' (from 'NF2(1)'), 'WF1' (from '(WF1)').\n"
    "   They must not be translated, renumbered, dropped, or used as substitutes for the\n"
    "   noun phrase.\n"
    "   Reference characters must NOT remain inside brackets: use 'substrate 100',\n"
    "   'electrode 100a', 'groove GR1', 'wafer structure WF1' — never 'substrate (100)',\n"
    "   'electrode (100a)', 'groove GR(1)', 'wafer structure (WF1)'.\n"
    "   Preserve pure math/variable symbols (T1, θ_k, BIT_3k) and genuine equation\n"
    "   notation that uses non-ASCII subscript brackets or math operators (e.g.\n"
    "   '〖BIT〗_(3k+1)') exactly as written.\n"
    "6. Patent style — formal USPTO language; no contractions; no casual phrasing.\n"
    "7. Possessives — for inanimate technical component relationships, prefer 'of'\n"
    "   constructions over apostrophe possessives, unless the apostrophe is part of a name.\n"
    "8. Glossary drift — terms in the established glossary must not deviate.\n"
    "9. Equation layout — [EQUATION] markers, including numbered forms, must remain in\n"
    "   the same order and relative position as the Korean source; do not regroup equations\n"
    "   separately from their following descriptions.\n"
    "Respond with valid JSON only.\n"
)


def build_decision_messages(
    section: str,
    pairs: list[tuple[str, str]],
    glossary: dict[str, str],
) -> list[dict]:
    paragraphs_block = "\n\n".join(
        f"[{i}]\nKorean: {kr}\nEnglish: {en}"
        for i, (kr, en) in enumerate(pairs)
    )
    glossary_block = format_for_prompt(glossary)
    user = (
        f"Section: {section}\n\n"
        f"GLOSSARY (terms that must be used consistently):\n{glossary_block}\n\n"
        f"{paragraphs_block}\n\n"
        "Identify any quality issues. For CLAIMS, be strict: flag any omitted\n"
        "element, step, dependency, modifier, reference character, equation marker,\n"
        "range, or condition. For BODY/DESCRIPTION, flag bracketed reference\n"
        "characters — both spaced forms like '(100)', '(WF1)', '[100]', and\n"
        "compact forms like 'GR(1)' or 'NF2(1)' — when they identify components.\n"
        "USPTO style places them after the noun phrase WITHOUT brackets\n"
        "('substrate 100', 'groove GR1', 'NF21'). Do not flag pure math/variable\n"
        "symbols or genuine equation notation like '〖BIT〗_(3k+1)'.\n"
        "Respond with JSON only — one of:\n"
        '  {"needs_revision": false, "issues": []}\n'
        '  {"needs_revision": true, "issues": ["<concise description>"]}'
    )
    return [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_revision_messages(
    section: str,
    pairs: list[tuple[str, str]],
    issues: list[str],
    glossary: dict[str, str],
) -> list[dict]:
    paragraphs_block = "\n\n".join(
        f"[{i}]\nKorean: {kr}\nEnglish: {en}"
        for i, (kr, en) in enumerate(pairs)
    )
    issues_block = "\n".join(f"- {issue}" for issue in issues)
    glossary_block = format_for_prompt(glossary)
    user = (
        f"Section: {section}\n\n"
        f"Issues to fix:\n{issues_block}\n\n"
        f"GLOSSARY (use these exact terms):\n{glossary_block}\n\n"
        f"{paragraphs_block}\n\n"
        "Return ONLY paragraphs that need changes as a JSON array.\n"
        "Omit paragraphs that are already correct.\n"
        "When revising claims, preserve every source limitation and do not shorten\n"
        "or summarize. When revising descriptions, remove brackets around reference\n"
        "characters in both spaced form ('(100)', '(WF1)') and compact form\n"
        "('GR(1)' → 'GR1', 'NF2(1)' → 'NF21'). Preserve pure math/variable symbols\n"
        "(T1, θ_k, BIT_3k) and genuine equation notation like '〖BIT〗_(3k+1)'.\n"
        'Format: [{"index": 0, "text": "<revised English text>"}, ...]'
    )
    return [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_single_claim_revision_messages(
    *,
    korean_claim: str,
    english_claim: str,
    issues: list[str],
    glossary: dict[str, str],
    required_opening: str | None = None,
) -> list[dict]:
    glossary_block = format_for_prompt(glossary)
    issues_block = "\n".join(f"- {issue}" for issue in issues) or "- Fix all visible claim-quality defects."
    opening_rule = (
        f"\nThe revised English claim must begin exactly with: {required_opening}\n"
        if required_opening else ""
    )
    return [
        {
            "role": "system",
            "content": (
                "You revise Korean-to-English patent claim translations for USPTO filing style. "
                "Output one plain English claim only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Revise this English claim using the Korean source.\n"
                "Preserve every Korean limitation, element, step, dependency, modifier, range, "
                "condition, bracketed/parenthetical phrase, reference character, and [EQUATION_N] marker. Do not summarize.\n"
                "Use strict USPTO claim style: comprising, wherein, further comprising, and "
                "proper dependent preambles. Do not output JSON, markdown, notes, or Korean.\n"
                f"{opening_rule}"
                f"Issues to fix:\n{issues_block}\n\n"
                f"Glossary:\n{glossary_block}\n\n"
                f"Korean claim:\n{korean_claim}\n\n"
                f"Current English claim:\n{english_claim}\n"
            ),
        },
    ]
