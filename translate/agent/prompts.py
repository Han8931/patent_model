"""Prompt builders for agent nodes — adds glossary injection on top of section prompts."""

from __future__ import annotations

from ..prompt import (
    PROMPT_ABSTRACT,
    PROMPT_BODY,
    PROMPT_CLAIMS,
    Prompt,
)
from .glossary import format_for_prompt


def _system_with_glossary(prompt: Prompt, glossary: dict[str, str]) -> str:
    """Inject the rolling glossary into the system message."""
    block = format_for_prompt(glossary)
    return prompt.system + (
        "\n"
        "ESTABLISHED TERMINOLOGY (reuse these EXACT English terms when the corresponding Korean term appears):\n"
        f"{block}\n"
    )


# ---------------------------------------------------------------------------
# Body — translate one chunk per call (chunk may span multiple paragraphs joined with \n)
# ---------------------------------------------------------------------------

def build_body_messages(chunk_text: str, glossary: dict[str, str]) -> list[dict]:
    system = _system_with_glossary(PROMPT_BODY, glossary)
    user = (
        "Translate the following Korean patent text into English (USPTO style).\n"
        "Render the entire text as ONE coherent English paragraph.\n"
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

def build_claim_messages(
    claim_num: int,
    chunk_text: str,
    glossary: dict[str, str],
    independent_categories: dict[int, str] | None = None,
) -> list[dict]:
    system = _system_with_glossary(PROMPT_CLAIMS, glossary)

    cats_block = ""
    if independent_categories:
        lines = "\n".join(f"  claim {n}: {c}" for n, c in independent_categories.items())
        cats_block = (
            "\nINDEPENDENT CLAIM CATEGORIES (use the matching <category> for dependent claims):\n"
            f"{lines}\n"
        )

    user = (
        f"Translate Korean claim {claim_num} into ONE coherent English claim sentence.\n"
        "- Render the FULL claim as a single sentence.\n"
        "- Place ';' between elements; insert a newline after each ';' and after each ':'.\n"
        "- Use lowercase after ':', ';', and 'wherein' (unless a proper noun).\n"
        "- Do NOT prepend the claim number — numbering is added separately.\n"
        "- If [EQUATION] appears, keep the marker verbatim AND translate every parameter\n"
        "  description that follows it (e.g. '여기서, A는 ..., B는 ..., C는 ...'). Output one\n"
        "  clause per parameter — do not merge, drop, or summarize any of them.\n"
        f"{cats_block}"
        "\n"
        "Output JSON ONLY:\n"
        '{"text": "<English claim sentence>", "category": "<method|apparatus|system|device|medium|...>", '
        '"is_independent": true|false, "key_terms": [{"ko": "...", "en": "..."}]}\n'
        "\n"
        "Korean:\n"
        f"{chunk_text}\n"
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
    "3. Claim structure — independent claims start with 'A <category> comprising:';\n"
    "   dependent claims start with 'The <category> of claim N, wherein'. NEVER 'according to claim'.\n"
    "4. Figure references — must be 'FIG. N' (uppercase, period after FIG).\n"
    "5. Patent style — formal USPTO language; no contractions; no casual phrasing.\n"
    "6. Glossary drift — terms in the established glossary must not deviate.\n"
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
