"""Prompt builders for agent nodes — adds glossary injection on top of section prompts."""

from __future__ import annotations

import re

from ..prompt import (
    CLAIM_PROMPT_BY_KIND,
    PROMPT_ABSTRACT,
    PROMPT_BODY,
    PROMPT_CLAIMS,
    PROMPT_CLAIMS_DEVICE,
    Prompt,
)
from .claim_classifier import (
    ClaimKind,
    MultiParent,
    PreambleSpec,
    build_dependent_preamble,
    dependent_adds_subject_matter,
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


def _system_with_glossary(
    prompt: Prompt,
    glossary: dict[str, str],
    *,
    claim_context: bool = False,
) -> str:
    """Inject the rolling glossary into the system message.

    For specification/body chunks, the glossary also acts as an antecedent
    ledger for earlier prose. For claims, it is terminology-only: terms from
    the description do not create claim antecedent basis.
    """
    block = format_for_prompt(glossary)
    if claim_context:
        return prompt.system + (
            "\n"
            "DOCUMENT TERMINOLOGY (Korean → English).\n"
            "Use this list ONLY to keep the same Korean technical term translated\n"
            "with the same English noun phrase throughout the document.\n"
            "IMPORTANT FOR CLAIM ARTICLES:\n"
            "  - A term appearing in the DESCRIPTION, ABSTRACT, or DRAWINGS does\n"
            "    NOT make that term already introduced in the CLAIMS.\n"
            "  - In each independent claim, introduce countable claim elements with\n"
            "    'a' or 'an' even if the same term appeared earlier in the description.\n"
            "  - Use 'the' only for terms already introduced earlier in the same\n"
            "    claim or in a parent claim referenced by the dependent claim.\n"
            f"{block}\n"
        )
    return prompt.system + (
        "\n"
        "ESTABLISHED TERMINOLOGY (Korean → English).\n"
        "Every term in this list has ALREADY BEEN INTRODUCED in an earlier\n"
        "chunk of this document, so within the current chunk:\n"
        "  - Reuse the EXACT English wording shown — do not paraphrase or\n"
        "    pluralize differently.\n"
        "  - Use 'the' (or a possessive) on every occurrence — NEVER 'a/an',\n"
        "    because these nouns are no longer first-mention.\n"
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

# ---------------------------------------------------------------------------
# Bulk-simple body prompt — chunk-level, minimal system message
# ---------------------------------------------------------------------------
# Mirrors the claims-bulk approach: one short instruction line, the glossary
# the claim translator already seeded, and the Korean text. Plain text out
# (no JSON), no retry, no key_terms — the claim-seeded glossary carries the
# antecedent-basis context that earlier per-chunk prompts tried to nudge via
# legend/respectively rules.

BULK_BODY_SIMPLE_SYSTEM = (
    "Translate the Korean patent specification into USPTO style. "
    "Preserve every [EQUATION_N] marker verbatim and in the same relative "
    "position. Use the established glossary for terminology — never deviate "
    "from a term that is already in the glossary. "
    "After the translation, add a final '===== GLOSSARY =====' banner and "
    "list any NEW Korean→English noun-phrase pairs you used that were not "
    "already in the glossary, one per line as 'korean → english'."
)


def build_body_simple_messages(
    chunk_text: str,
    glossary: dict[str, str],
) -> list[dict]:
    system = BULK_BODY_SIMPLE_SYSTEM
    if glossary:
        system += (
            "\n\nGLOSSARY (Korean → English):\n"
            + format_for_prompt(glossary)
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": chunk_text},
    ]


def parse_body_simple_response(raw: str) -> tuple[str, dict[str, str]]:
    """Split a body-simple response into (translation_text, new_glossary).

    Tolerant: if no '===== GLOSSARY =====' banner is found, treats the entire
    response as the translation and returns an empty glossary dict.
    """
    import re as _re
    text = _strip_bulk_response_fences(raw)
    match = _re.search(_BULK_GLOSSARY_BANNER_RE, text)
    if not match:
        return text.strip(), {}
    translation = text[: match.start()].strip()
    block = text[match.end():].strip()
    out: dict[str, str] = {}
    line_re = _re.compile(r"^\s*(.+?)\s*(?:→|->)\s*(.+?)\s*$")
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        m = line_re.match(line)
        if not m:
            continue
        ko, en = m.group(1).strip(), m.group(2).strip()
        if ko and en and "→" not in en and "->" not in en:
            out[ko] = en
    return translation, out


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
        "Output JSON ONLY in this schema:\n"
        '{"text": "<English translation>", "key_terms": [{"ko": "<Korean term>", "en": "<English term>"}]}\n'
        "key_terms must list significant technical noun phrases you translated (components, materials, processes).\n"
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
        "- Do not output JSON schema text, key_terms-only text, commentary, or markdown.\n"
        "- If the source paragraph originally had a numeric paragraph ID such as [001], "
        "preserve it only if it appears in the text shown below.\n"
        "- Return JSON ONLY with real English text:\n"
        '{"text": "<complete English paragraph>", "key_terms": []}\n'
        "\n"
        "Korean paragraph to translate:\n"
        f"{chunk_text}\n"
    )
    return previous_messages + [{"role": "user", "content": retry}]


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
        "- Return JSON ONLY: "
        '{"text": "<English fragment>", "key_terms": []}\n'
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
        "- Return JSON ONLY: "
        '{"text": "<symbol> is <description>", "key_terms": []}\n'
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
        "Output JSON ONLY:\n"
        '{"text": "<English abstract>", "key_terms": [{"ko": "...", "en": "..."}]}\n'
        "\n"
        "Korean:\n"
        f"{chunk_text}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
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
        "Output JSON ONLY:\n"
        '{"text": "<English claim sentence>", "key_terms": [{"ko": "...", "en": "..."}]}\n'
        "\n"
        "Korean:\n"
        f"{chunk_text}\n"
    )


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
    else:  # device, system
        if dependent_adds_subject_matter(chunk_text):
            opener = f"'{preamble}, further comprising <added element> ...'"
        else:
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
        "  - If the Korean dependent claim uses addition cues such as '더', "
        "'추가로', '부가적으로', '또한', or '나아가' with inclusion/provision "
        "verbs such as '포함', '구비', or '제공', use 'further comprising' "
        "rather than 'wherein'.\n"
        "\n"
        + _SHARED_CLAIM_RULES
        + _claim_equation_rules(chunk_text, equation_context)
        + layout_repair
        + _format_equation_context(equation_context)
        + "\n"
        "Output JSON ONLY:\n"
        '{"text": "<English claim sentence>", "key_terms": [{"ko": "...", "en": "..."}]}\n'
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
    system = _system_with_glossary(prompt, glossary, claim_context=True)

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
        "- Do not output JSON schema text, key_terms-only text, commentary, or markdown.\n"
        "- Return JSON ONLY with a real English claim in the text field:\n"
        '{"text": "<complete English claim sentence>", "key_terms": []}\n'
        "\n"
        "Korean claim to translate:\n"
        f"{chunk_text}\n"
    )
    return previous_messages + [{"role": "user", "content": retry}]


# ---------------------------------------------------------------------------
# Bulk claim translation — single LLM call for ALL claims at once
# ---------------------------------------------------------------------------
# This intentionally uses a minimal prompt to test how well the model handles
# claim drafting unaided. Image-based equations are represented as
# [EQUATION_N] markers in the input; the prompt requires those markers to
# appear unchanged at the matching positions in the output so the writer can
# splice the original equation XML back in.

_BULK_CLAIMS_DELIMITER = "===== CLAIM {n} ====="
_BULK_CLAIMS_DELIM_RE_FMT = r"=====\s*CLAIM\s+(\d+)\s*====="
_BULK_GLOSSARY_BANNER = "===== GLOSSARY ====="
_BULK_GLOSSARY_BANNER_RE = r"=====\s*GLOSSARY\s*====="

BULK_CLAIMS_SYSTEM = (
    "Translate these claims into USPTO style. "
    "Preserve every [EQUATION_N] marker verbatim and in the same relative "
    "position. Return each claim's English under the same '===== CLAIM N ====='"
    " banner that precedes it in the input. "
    "After the last claim, add a final '===== GLOSSARY =====' banner and list "
    "the key Korean→English noun-phrase pairs you used (components, materials, "
    "processes), one per line as 'korean → english'. The glossary seeds the "
    "description translator so it can keep terminology consistent with the claims."
)


def build_claims_bulk_messages(
    claims: list[tuple[int, str]],
) -> list[dict]:
    """Bundle every claim into a single user message keyed by claim number.

    ``claims`` is a list of (claim_num, korean_text) pairs in source order.
    The model returns the same banner format; ``parse_claims_bulk_response``
    splits it back into per-claim translations and ``parse_claims_bulk_glossary``
    extracts the trailing Korean→English glossary.
    """
    parts: list[str] = []
    for num, text in claims:
        parts.append(_BULK_CLAIMS_DELIMITER.format(n=num))
        parts.append(text)
    user = "\n".join(parts)
    return [
        {"role": "system", "content": BULK_CLAIMS_SYSTEM},
        {"role": "user", "content": user},
    ]


def _strip_bulk_response_fences(raw: str | None) -> str:
    import re as _re
    if not raw:
        return ""
    text = raw.strip()
    if text.startswith("```"):
        text = _re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = _re.sub(r"\n```\s*$", "", text)
    return text


def parse_claims_bulk_response(raw: str) -> dict[int, str]:
    """Split the model's bulk response back into ``{claim_num: english}``.

    The trailing '===== GLOSSARY =====' section (if present) is truncated off
    the last claim's text so glossary lines don't leak into the final claim.
    """
    import re as _re
    text = _strip_bulk_response_fences(raw)
    glossary_match = _re.search(_BULK_GLOSSARY_BANNER_RE, text)
    body = text[: glossary_match.start()] if glossary_match else text

    banner_re = _re.compile(_BULK_CLAIMS_DELIM_RE_FMT)
    out: dict[int, str] = {}
    matches = list(banner_re.finditer(body))
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out[num] = body[start:end].strip()
    return out


def parse_claims_bulk_glossary(raw: str) -> dict[str, str]:
    """Extract the trailing '===== GLOSSARY =====' section as ``{ko: en}``.

    Each glossary line is expected as ``korean → english`` (with the proper
    '→' arrow or an ASCII '->' fallback). Lines that don't match are skipped.
    Empty input or missing glossary banner returns ``{}``.
    """
    import re as _re
    text = _strip_bulk_response_fences(raw)
    match = _re.search(_BULK_GLOSSARY_BANNER_RE, text)
    if not match:
        return {}
    block = text[match.end():].strip()
    out: dict[str, str] = {}
    line_re = _re.compile(r"^\s*(.+?)\s*(?:→|->)\s*(.+?)\s*$")
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        m = line_re.match(line)
        if not m:
            continue
        ko, en = m.group(1).strip(), m.group(2).strip()
        if ko and en and "→" not in en and "->" not in en:
            out[ko] = en
    return out


# ---------------------------------------------------------------------------
# Review — decide + revise per section
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM = (
    "You are a senior patent translation reviewer (Korean → English).\n"
    "Assess translated patent text for the following issues:\n"
    "1. Terminology consistency — same Korean term must map to same English term.\n"
    "2. Translation accuracy — no omissions, additions, or hallucinations.\n"
    "3. Claim structure — independent claims start with 'A <category> comprising:';\n"
    "   dependent claims start with 'The <category> of claim N, wherein'. NEVER 'according to claim'.\n"
    "4. Figure references — must be 'FIG. N' (uppercase, period after FIG).\n"
    "5. Patent style — formal USPTO language; no contractions; no casual phrasing.\n"
    "6. Glossary drift — terms in the established glossary must not deviate.\n"
    "7. Equation layout — [EQUATION] markers, including numbered forms, must remain in\n"
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
        "Identify any quality issues. Respond with JSON only — one of:\n"
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
        'Format: [{"index": 0, "text": "<revised English text>"}, ...]'
    )
    return [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user", "content": user},
    ]
