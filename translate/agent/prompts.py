"""Prompt builders — every system prompt is a single sentence.

Every translation-task prompt is one line: *"Translate ... in USPTO style ..."*.
The USPTO drafting rules (bracket policy, FIG. casing, antecedent basis,
'comprising' discipline, parameter-legend layout, reference numerals) are
all enforced **by the postprocess sweeps** in ``docx_utils.py`` and the
deterministic claim helpers in ``nodes.translate_claims``, not by long
prompts. The model only needs to know the high-level task.

Body and abstract use a ``===== GLOSSARY =====`` trailer convention:
the model appends a Korean→English glossary block after the translation so
the rolling glossary can extend chunk-to-chunk. The bulk claims path uses
the same trailer.
"""

from __future__ import annotations

import re

from .glossary import format_for_prompt


# ---------------------------------------------------------------------------
# Shared helpers — glossary block + GLOSSARY-trailer parser.
# ---------------------------------------------------------------------------

_GLOSSARY_BANNER_RE = r"={3,}\s*GLOSSARY\s*={3,}"


def _glossary_block(glossary: dict[str, str]) -> str:
    """Inline 'GLOSSARY (Korean → English): ...' block, empty if no glossary."""
    if not glossary:
        return ""
    return "\n\nGLOSSARY (Korean → English):\n" + format_for_prompt(glossary)


def _strip_fences(raw: str | None) -> str:
    if not raw:
        return ""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    return text


def _parse_glossary_lines(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    line_re = re.compile(r"^\s*(.+?)\s*(?:→|->)\s*(.+?)\s*$")
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


def parse_translation_with_glossary(raw: str) -> tuple[str, dict[str, str]]:
    """Split a response into (translation_text, new_glossary).

    Used by every translator that requests a trailing ``===== GLOSSARY ====='`'
    block. Missing trailer ⇒ whole response is the translation, empty glossary.
    """
    text = _strip_fences(raw)
    match = re.search(_GLOSSARY_BANNER_RE, text)
    if not match:
        return text.strip(), {}
    translation = text[: match.start()].strip()
    block = text[match.end():].strip()
    return translation, _parse_glossary_lines(block)


# ---------------------------------------------------------------------------
# Body — one chunk per call.
# ---------------------------------------------------------------------------

BODY_SYSTEM = (
    "Translate this Korean patent text to English in USPTO style. "
    "Use the glossary below for terminology. Keep every [EQUATION_N] marker "
    "verbatim. After the translation, list any NEW Korean→English terms "
    "you used under a '===== GLOSSARY =====' banner."
)


def build_body_messages(
    chunk_text: str,
    glossary: dict[str, str],
    equation_context: dict[str, str] | None = None,
) -> list[dict]:
    return [
        {"role": "system", "content": BODY_SYSTEM + _glossary_block(glossary)},
        {"role": "user", "content": chunk_text},
    ]


def build_body_retry_messages(
    *,
    previous_messages: list[dict],
    problem: str,
    chunk_text: str,
) -> list[dict]:
    retry = (
        f"Previous response unusable ({problem}). "
        "Retry: translate the Korean below to English in USPTO style. "
        "No Korean characters in the output."
    )
    return previous_messages + [{"role": "user", "content": retry}]


# ---------------------------------------------------------------------------
# Sentence-level — fragments inside equation-bearing paragraphs.
# Plain text out, no glossary trailer (caller stitches around markers).
# ---------------------------------------------------------------------------

SEGMENT_SYSTEM = (
    "Translate this Korean patent text fragment to English in USPTO style. "
    "Do not translate any [EQUATION_N] marker — the surrounding code stitches "
    "markers in separately. Output plain text only."
)


def build_segment_messages(
    korean_segment: str,
    glossary: dict[str, str],
) -> list[dict]:
    return [
        {"role": "system", "content": SEGMENT_SYSTEM + _glossary_block(glossary)},
        {"role": "user", "content": korean_segment},
    ]


CLAUSE_SYSTEM = (
    "Translate this Korean parameter clause to one English clause in USPTO "
    "style: '<symbol> is <description>'. Keep the symbol VERBATIM as the "
    "first token. Plain text only — no surrounding punctuation, no 'where' / "
    "'wherein' wrapper."
)


def build_clause_messages(
    symbol: str,
    korean_clause: str,
    glossary: dict[str, str],
) -> list[dict]:
    return [
        {"role": "system", "content": CLAUSE_SYSTEM + _glossary_block(glossary)},
        {"role": "user", "content": f"Symbol: {symbol}\nKorean: {korean_clause}"},
    ]


# ---------------------------------------------------------------------------
# Abstract — one concise paragraph.
# ---------------------------------------------------------------------------

ABSTRACT_SYSTEM = (
    "Translate this Korean patent abstract to one concise English paragraph "
    "in USPTO style. After the translation, list any NEW Korean→English "
    "terms you used under a '===== GLOSSARY =====' banner."
)


def build_abstract_messages(chunk_text: str, glossary: dict[str, str]) -> list[dict]:
    return [
        {"role": "system", "content": ABSTRACT_SYSTEM + _glossary_block(glossary)},
        {"role": "user", "content": chunk_text},
    ]


# ---------------------------------------------------------------------------
# Claims — single bundled LLM call for ALL claims.
# ---------------------------------------------------------------------------

_BULK_CLAIMS_DELIMITER = "===== CLAIM {n} ====="
_BULK_CLAIMS_DELIM_RE = r"={3,}\s*CLAIM\s+(\d+)\s*={3,}"

BULK_CLAIMS_SYSTEM = (
    "Translate each Korean claim to English in USPTO style. "
    "Return each claim's English under the same '===== CLAIM N =====' banner "
    "that precedes it; keep every [EQUATION_N] marker verbatim. "
    "After the last claim, list the Korean→English terms you used under a "
    "'===== GLOSSARY =====' banner, one per line as 'korean → english'."
)


def build_claims_bulk_messages(
    claims: list[tuple[int, str]],
) -> list[dict]:
    parts: list[str] = []
    for num, text in claims:
        parts.append(_BULK_CLAIMS_DELIMITER.format(n=num))
        parts.append(text)
    return [
        {"role": "system", "content": BULK_CLAIMS_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


def parse_claims_bulk_response(raw: str) -> dict[int, str]:
    """Split the bulk response into ``{claim_num: english}``, dropping any
    trailing GLOSSARY block so its lines don't leak into the last claim."""
    text = _strip_fences(raw)
    glossary_match = re.search(_GLOSSARY_BANNER_RE, text)
    body = text[: glossary_match.start()] if glossary_match else text

    banner_re = re.compile(_BULK_CLAIMS_DELIM_RE)
    out: dict[int, str] = {}
    matches = list(banner_re.finditer(body))
    for i, m in enumerate(matches):
        num = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out[num] = body[start:end].strip()
    return out


def parse_claims_bulk_glossary(raw: str) -> dict[str, str]:
    """Extract the trailing '===== GLOSSARY =====' section as ``{ko: en}``."""
    text = _strip_fences(raw)
    match = re.search(_GLOSSARY_BANNER_RE, text)
    if not match:
        return {}
    return _parse_glossary_lines(text[match.end():].strip())


# ---------------------------------------------------------------------------
# Review — decide + revise per section. JSON in, JSON out.
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM = (
    "You are a Korean→English patent translation reviewer. Check terminology "
    "consistency against the glossary, translation accuracy (no omissions or "
    "hallucinations), USPTO claim structure, FIG. casing, and [EQUATION_N] "
    "marker positions. Respond with valid JSON only."
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
    user = (
        f"Section: {section}"
        f"{_glossary_block(glossary)}\n\n"
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
    user = (
        f"Section: {section}\n\n"
        f"Issues to fix:\n{issues_block}"
        f"{_glossary_block(glossary)}\n\n"
        f"{paragraphs_block}\n\n"
        "Return ONLY paragraphs that need changes as a JSON array. Omit "
        "paragraphs that are already correct.\n"
        'Format: [{"index": 0, "text": "<revised English text>"}, ...]'
    )
    return [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user", "content": user},
    ]
