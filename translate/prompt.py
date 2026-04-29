from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

# ---------------------------------------------------------------------------
# Shared rule blocks
# ---------------------------------------------------------------------------

_BRACKET_RULES = (
    "\n"
    "BRACKET / BRACE TRANSLATION POLICY:\n"
    "1) Square bracket markers with 4 digits '[0016]' are paragraph IDs — do NOT translate or remove them.\n"
    "2) Korean headings in corner brackets '【...】': translate the text, remove '【】' in output.\n"
    "   Examples: '【발명의 명칭】' → 'TITLE OF THE INVENTION', '【요약】' → 'ABSTRACT'.\n"
    "3) Curly braces '{...}': if content is already English, keep it as-is.\n"
    "   Example: '반도체 패키지{SEMICONDUCTOR PACKAGE}' → 'Semiconductor package {SEMICONDUCTOR PACKAGE}'.\n"
    "4) Digit-only parentheses e.g. (1000): remove parentheses, keep the numeral.\n"
    "   '반도체 패키지(1000)' → 'a semiconductor package 1000'.\n"
    "   Non-digit parentheses like (CPO), (AI), (see FIG. 1): keep as-is.\n"
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
        "- Independent claims: preamble pattern 'A <category> comprising:'.\n"
        "- Dependent claims: start with 'The <category> of claim <N>, wherein ...'.\n"
        "  NEVER use 'according to claim', 'as claimed in', or 'pursuant to claim'.\n"
        "- Maintain antecedent basis: introduce with 'a/an', refer back with 'the'.\n"
        "- Use 'wherein' (not 'where') for limitations.\n"
        "- Keep each claim as one sentence using semicolons.\n"
        "- Preserve claim numbers exactly.\n"
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
    "Respond with valid JSON only. No commentary, no markdown fences outside the JSON.\n"
)


def build_batch_messages(section: str, prompt: "Prompt", items: list[str]) -> list[dict]:
    """Build messages for batch translation of a list of paragraphs."""
    numbered = "\n\n".join(f"[{i}] {text}" for i, text in enumerate(items))
    user_content = (
        "Translate each numbered Korean patent paragraph into English.\n"
        "Return translations in the SAME numbered format [0], [1], [2]…\n"
        "Output ONLY the numbered translations — no commentary, no explanations.\n\n"
        f"{numbered}"
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
