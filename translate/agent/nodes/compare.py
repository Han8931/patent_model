"""Source/translation comparison pass.

This is a post-translation quality gate.  The normal validators catch obvious
bad output (Korean left behind, missing reference characters, equation marker
loss), but they cannot see whether a fluent English paragraph omitted a source
sentence.  This node asks the LLM to compare each Korean source chunk against
its current English translation and, only when needed, return a corrected full
translation.
"""

from __future__ import annotations

from ..docx_utils import postprocess
from ..glossary import clean_translation_text, extract_json_block
from ..state import Chunk, TranslationState
from ..validation import coverage_problem, translation_problem
from .review import _claim_preamble_specs, _validate_claim_revision


def _system(kind: str) -> str:
    return (
        "You are a patent translation quality reviewer. Compare the Korean "
        "source with the English translation. Your job is to find omissions, "
        "mistranslated limitations, missing sentences/phrases, missing reference "
        "characters, and changed equation markers. Do not improve style unless it "
        "fixes a real fidelity problem. Preserve USPTO patent drafting style. "
        "Return ONLY valid JSON."
        f"\nSection: {kind.upper()}"
    )


def _messages(chunk: Chunk, glossary: dict) -> list[dict]:
    glossary_text = "\n".join(f"{k} -> {v}" for k, v in list(glossary.items())[:80]) or "(none)"
    return [
        {"role": "system", "content": _system(chunk.kind)},
        {
            "role": "user",
            "content": (
                "Compare SOURCE and CURRENT_TRANSLATION.\n"
                "If CURRENT_TRANSLATION is complete and faithful, return:\n"
                '{"needs_revision": false, "issues": [], "revised_text": ""}\n\n'
                "If anything is omitted or mistranslated, return a corrected FULL "
                "English translation in revised_text. Do not return only a patch.\n"
                "Preserve all reference numerals/characters, including pure-letter "
                "references such as LD and GC. Preserve [EQUATION_N] markers exactly.\n"
                "For claims, keep the claim number and USPTO claim preamble style.\n\n"
                f"ESTABLISHED_TERMINOLOGY:\n{glossary_text}\n\n"
                f"SOURCE:\n{chunk.text}\n\n"
                f"CURRENT_TRANSLATION:\n{chunk.translation or ''}\n\n"
                "Return JSON shape exactly: "
                '{"needs_revision": boolean, "issues": ["..."], "revised_text": "..."}'
            ),
        },
    ]


def _clean_revised(chunk: Chunk, revised: str, specs: dict[int, object]) -> tuple[str, str]:
    text = clean_translation_text(revised)
    if not text:
        return "", "The comparison pass returned empty revised_text."

    if chunk.kind == "claim":
        cleaned, problem = _validate_claim_revision(chunk, text, specs)  # includes postprocess
    else:
        cleaned = postprocess(text)
        problem = translation_problem(cleaned)
    if problem:
        return cleaned, problem

    problem = coverage_problem(chunk.text, cleaned)
    if problem:
        return cleaned, problem
    return cleaned, ""


def _compare_one(chunk: Chunk, *, client, glossary: dict, specs: dict[int, object], verbose: bool) -> bool:
    """Return True if a revision was applied."""
    if getattr(chunk, "applied_in_place", False):
        return False
    if not chunk.translation:
        return False

    messages = _messages(chunk, glossary)
    last_problem = ""
    for attempt in range(3):
        try:
            raw = client.complete(messages)
            parsed = extract_json_block(raw)
            if not isinstance(parsed, dict):
                last_problem = "Comparison response was not valid JSON."
                raise ValueError(last_problem)

            needs = bool(parsed.get("needs_revision"))
            issues = parsed.get("issues") if isinstance(parsed.get("issues"), list) else []
            if not needs:
                return False

            revised = parsed.get("revised_text") or parsed.get("text") or ""
            cleaned, problem = _clean_revised(chunk, revised, specs)
            if not problem:
                chunk.translation = cleaned
                if verbose:
                    print(f"  [COMPARE {chunk.id}] revised: {'; '.join(map(str, issues[:3]))}")
                return True
            last_problem = problem
        except Exception as exc:
            if not last_problem:
                last_problem = f"Comparison call failed: {type(exc).__name__}: {exc}"

        if attempt < 2:
            messages = messages + [{
                "role": "user",
                "content": (
                    "Try again. Return only valid JSON with a corrected FULL English "
                    f"translation in revised_text. Problem: {last_problem}"
                ),
            }]

    if verbose:
        print(f"  [COMPARE {chunk.id}] skipped revision: {last_problem}")
    return False


def compare_translations(state: TranslationState) -> dict:
    """LLM comparison pass over claims/body/abstract before DOCX writing."""
    if not state.get("review", True):
        return {}

    client = state["client"]
    glossary = state.get("glossary", {})
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)

    chunks: list[Chunk] = []
    chunks.extend(state.get("chunks_claims", []))
    chunks.extend(state.get("chunks_body", []))
    chunks.extend(state.get("chunks_abstract", []))
    chunks = [c for c in chunks if c.translation and not getattr(c, "applied_in_place", False)]
    if not chunks:
        return {}

    progress(f"Comparing source and translations ({len(chunks)} chunk(s))…")
    specs = _claim_preamble_specs(state.get("chunks_claims", []))
    revised = 0
    for i, chunk in enumerate(chunks, 1):
        if _compare_one(chunk, client=client, glossary=glossary, specs=specs, verbose=verbose):
            revised += 1
        if i % 10 == 0 or i == len(chunks):
            progress(f"  COMPARE {i}/{len(chunks)}")

    if revised:
        progress(f"Comparison pass revised {revised} chunk(s).")
    return {
        "chunks_claims": state.get("chunks_claims", []),
        "chunks_body": state.get("chunks_body", []),
        "chunks_abstract": state.get("chunks_abstract", []),
    }
