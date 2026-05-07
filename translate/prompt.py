from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

# ---------------------------------------------------------------------------
# Shared rule blocks
# ---------------------------------------------------------------------------

_BRACKET_RULES = (
    "\n"
    "BRACKET / BRACE TRANSLATION POLICY:\n"
    "1) Square bracket markers with 4–5 digits '[0016]' are paragraph IDs — do NOT translate or remove them.\n"
    "2) Korean headings in corner brackets '【...】': translate the text, remove '【】' in output.\n"
    "   Examples: '【발명의 명칭】' → 'TITLE OF THE INVENTION', '【요약】' → 'ABSTRACT'.\n"
    "3) Curly braces '{...}': if content is already English, keep it as-is.\n"
    "   Example: '반도체 패키지{SEMICONDUCTOR PACKAGE}' → 'Semiconductor package {SEMICONDUCTOR PACKAGE}'.\n"
    "4) DRAWING REFERENCE NUMERALS — digit-only parentheses:\n"
    "   - If parentheses contain ONLY digits, remove the parentheses and keep the numeral.\n"
    "   - '반도체 패키지(1000)' → 'a semiconductor package 1000'\n"
    "   - '패키지 기판(100)' → 'a package substrate 100'\n"
    "   - Do NOT apply this rule when parentheses contain any non-digit characters.\n"
    "   - Keep as-is: (CPO), (AI), (Optic Engine Unit: OEU), (see FIG. 1).\n"
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

_HEADING_RULES = (
    "\n"
    "HEADING NORMALIZATION:\n"
    "- '【요약】' or '요약서' → 'ABSTRACT'\n"
    "- '【청구 범위】' or '【청구범위】' → 'CLAIMS'\n"
    "- Remove '【】' characters; output heading as plain text.\n"
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
)

# ---------------------------------------------------------------------------
# User template (shared across all section prompts)
# ---------------------------------------------------------------------------

_USER_TEMPLATE = (
    "Translate the following Korean patent paragraph into English "
    "following USPTO patent application style.\n"
    "\n"
    "Korean:\n"
    "{text}\n"
    "\n"
    "English translation:"
)

# ---------------------------------------------------------------------------
# Prompt dataclass + registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Prompt:
    name: str
    system: str
    user: str = _USER_TEMPLATE

    def build_messages(
        self,
        korean_text: str,
        context: list[tuple[str, str]] | None = None,
        lookahead: list[str] | None = None,
    ) -> list[dict]:
        """Build the chat message list for one translation call.

        Rolling context pairs (korean, english) are injected as prior
        user/assistant turns so the model stays consistent on terminology.
        Lookahead strings are appended to the current user message as
        read-only upcoming context.
        """
        messages: list[dict] = [{"role": "system", "content": self.system}]
        for kr, en in (context or []):
            messages.append({"role": "user", "content": self.user.format(text=kr)})
            messages.append({"role": "assistant", "content": en})

        user_content = self.user.format(text=korean_text)
        if lookahead:
            upcoming = "\n".join(lookahead)
            lookahead_block = (
                f"\n[Upcoming paragraphs — for context only, do NOT translate:]\n{upcoming}"
            )
            marker = "\n\nEnglish translation:"
            if marker in user_content:
                idx = user_content.rfind(marker)
                user_content = user_content[:idx] + lookahead_block + user_content[idx:]
            else:
                user_content += lookahead_block

        messages.append({"role": "user", "content": user_content})
        return messages


PROMPTS: Dict[str, Prompt] = {}


def register_prompt(p: Prompt) -> Prompt:
    PROMPTS[p.name] = p
    return p


# ---------------------------------------------------------------------------
# Section-specific prompts
# ---------------------------------------------------------------------------

PROMPT_BODY = register_prompt(Prompt(
    name="body",
    system=(
        COMMON_SYSTEM
        + "\n"
        "SECTION: DESCRIPTION / DETAILED DESCRIPTION / BACKGROUND / DRAWINGS\n"
        "- Keep technical explanations precise and complete.\n"
        "- Use consistent noun phrases for components (e.g., 'a substrate', 'a passivation layer').\n"
        "- Preserve all reference numerals exactly (e.g., 100a, GR(1), T1).\n"
        "- Do not add advantages or conclusions unless explicitly stated in the source.\n"
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
        "- The number of [EQUATION_N] tokens you emit MUST equal the number you received.\n"
    ),
))

PROMPT_ABSTRACT = register_prompt(Prompt(
    name="abstract",
    system=(
        COMMON_SYSTEM
        + "\n"
        "SECTION: ABSTRACT\n"
        "- Keep the translation concise, typically a single paragraph.\n"
        "- Focus on: what the invention is, key components, and core operation.\n"
        "- Do not include legal arguments, advantages, or marketing language.\n"
    ),
))

PROMPT_CLAIMS = register_prompt(Prompt(
    name="claims",
    system=(
        COMMON_SYSTEM
        + "\n"
        "SECTION: CLAIMS\n"
        "\n"
        "INDEPENDENT CLAIMS:\n"
        "- Preamble pattern: 'A <category> comprising:' or 'A <category> performed by <actor>, the <category> comprising:'.\n"
        "- List elements separated by ';' and '; and' before the last element.\n"
        "\n"
        "DEPENDENT CLAIMS — KOREAN PHRASE CONVERSION (CRITICAL):\n"
        "- Korean dependency phrases and their required English forms:\n"
        "  * '청구항 <M>에 있어서,' → 'The <category> of claim <M>, wherein ...'\n"
        "  * '제 <M> 항에 있어서,' → 'The <category> of claim <M>, wherein ...'\n"
        "  * '청구항 <M>에 따른' → 'The <category> of claim <M>, ...'\n"
        "  The <category> must match the category of the independent claim it depends on.\n"
        "\n"
        "FORBIDDEN PHRASES (ABSOLUTE BAN — never use any of these):\n"
        "  * 'according to claim'\n"
        "  * 'as claimed in claim'\n"
        "  * 'pursuant to claim'\n"
        "  * 'in accordance with claim'\n"
        "  The ONLY allowed reference style is: 'The <category> of claim <N>, ...'\n"
        "\n"
        "EQUATIONS INSIDE A CLAIM:\n"
        "- [EQUATION] markers, including numbered markers like [EQUATION_1], stand in for\n"
        "  Word equations; keep each marker exactly where it appears.\n"
        "- Preserve the source order and layout around equations. Do not collect all equations\n"
        "  first and then list all descriptions afterward.\n"
        "- If the source alternates equation and legend/description, keep the translation in\n"
        "  that same alternating order, e.g. [EQUATION_1] + its description, then\n"
        "  [EQUATION_2] + its description.\n"
        "- A parameter legend usually follows the equation (Korean: '여기서, A는 ..., B는 ...').\n"
        "  Render it after the equation as: 'where A is ...; B is ...; and C is ...,' — one\n"
        "  clause per parameter, joined by ';' and a newline (per the FORMATTING rules below).\n"
        "- Translate EVERY parameter description. The output must contain one clause for each\n"
        "  '<symbol>는/은 ...' item in the source. Do not merge multiple parameters into one\n"
        "  clause and do not drop any. If the source lists 5 parameters, output 5 clauses.\n"
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
    ),
))

# Section name → prompt routing table
SECTION_PROMPTS: Dict[str, Prompt] = {
    "DESCRIPTION":                      PROMPT_BODY,
    "TITLE OF INVENTION":               PROMPT_BODY,
    "BRIEF DESCRIPTION OF THE DRAWINGS": PROMPT_BODY,
    "DETAILED DESCRIPTION OF EMBODIMENTS": PROMPT_BODY,
    "TECHNICAL FIELD":                  PROMPT_BODY,
    "BACKGROUND ART":                   PROMPT_BODY,
    "TECHNICAL PROBLEM":                PROMPT_BODY,
    "SOLUTION TO PROBLEM":              PROMPT_BODY,
    "ADVANTAGEOUS EFFECTS OF INVENTION": PROMPT_BODY,
    "ABSTRACT":                         PROMPT_ABSTRACT,
    "CLAIMS":                           PROMPT_CLAIMS,
}

DEFAULT_PROMPT = PROMPT_BODY


# ---------------------------------------------------------------------------
# Review phase — decision node + revision
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM = (
    "You are a senior patent translation reviewer (Korean → English).\n"
    "Assess translated patent paragraphs for the following issues:\n"
    "1. Terminology consistency — the same Korean term must map to the same English term throughout.\n"
    "2. Translation accuracy — no omissions, additions, or hallucinations relative to the Korean.\n"
    "3. Claim structure — independent claims must start with 'A <category> comprising:';\n"
    "   dependent claims must start with 'The <category> of claim N, wherein'.\n"
    "4. Figure references — must be 'FIG. N' (uppercase, period after FIG), never 'Figure N' or 'fig N'.\n"
    "5. Patent style — formal USPTO language; no contractions or casual phrasing.\n"
    "6. Equation layout — [EQUATION] markers, including numbered forms, must remain in the\n"
    "   same order and relative position as the Korean source; do not regroup equations\n"
    "   separately from their following descriptions.\n"
    "Respond with valid JSON only. No commentary, no markdown fences outside the JSON.\n"
)


def build_batch_messages(section: str, prompt: "Prompt", items: list[str]) -> list[dict]:
    """Build messages for batch translation of a list of paragraphs."""
    numbered = "\n\n".join(f"[{i}] {text}" for i, text in enumerate(items))

    if section == "CLAIMS":
        extra = (
            "Do NOT prepend claim numbers (e.g. '1.', 'Claim 1.', 'Claims 1.')\n"
            "— numbering is handled separately.\n"
            "Output exactly one translation per input item. Do not merge or skip items.\n"
        )
    else:
        extra = ""

    user_content = (
        "Translate each numbered Korean patent paragraph into English.\n"
        "Return translations in the SAME numbered format [0], [1], [2]…\n"
        "Output ONLY the numbered translations — no commentary, no explanations.\n"
        + extra
        + f"\n{numbered}"
    )
    return [
        {"role": "system", "content": prompt.system},
        {"role": "user", "content": user_content},
    ]


def build_decision_messages(section: str, pairs: list[tuple[str, str]]) -> list[dict]:
    """Build the decision-node prompt: should this section be revised?"""
    paragraphs_block = "\n\n".join(
        f"[{i}]\nKorean: {kr}\nEnglish: {en}"
        for i, (kr, en) in enumerate(pairs)
    )
    user_content = (
        f"Section: {section}\n\n"
        f"{paragraphs_block}\n\n"
        f"Identify any quality issues in the translations above.\n"
        f"Respond with JSON only — one of:\n"
        f'  {{"needs_revision": false, "issues": []}}\n'
        f'  {{"needs_revision": true, "issues": ["<concise description of each issue>"]}}'
    )
    return [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user", "content": user_content},
    ]


def build_revision_messages(
    section: str,
    pairs: list[tuple[str, str]],
    issues: list[str],
) -> list[dict]:
    """Build the revision prompt: fix the identified issues."""
    paragraphs_block = "\n\n".join(
        f"[{i}]\nKorean: {kr}\nEnglish: {en}"
        for i, (kr, en) in enumerate(pairs)
    )
    issues_block = "\n".join(f"- {issue}" for issue in issues)
    user_content = (
        f"Section: {section}\n\n"
        f"Issues to fix:\n{issues_block}\n\n"
        f"{paragraphs_block}\n\n"
        f"Return ONLY paragraphs that need changes as a JSON array.\n"
        f"Omit paragraphs that are already correct.\n"
        f'Format: [{{"index": 0, "text": "revised English text"}}, ...]'
    )
    return [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user", "content": user_content},
    ]
