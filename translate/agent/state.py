"""State definitions for the translation agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

from ..client import LLMClient


ParagraphKind = Literal["blank", "image", "section_header", "claim_header", "text"]


@dataclass
class ParagraphRecord:
    """One docx paragraph with its classification."""
    index: int
    kind: ParagraphKind
    para: Any                      # docx paragraph object
    raw: str = ""
    section: str | None = None
    mapped: str | None = None      # for section_header: English label
    claim_num: int | None = None
    mixed: bool = False            # text + image/equation in same paragraph


ClaimKind = Literal["device", "method", "crm", "system"]
MultiParent = Literal["single", "or", "range"]


@dataclass
class Chunk:
    """A unit of text sent to the LLM. May span multiple paragraphs."""
    id: str
    section: str
    kind: Literal["body", "abstract", "claim"]
    paragraph_indices: list[int]   # indices into state.records
    text: str                      # joined Korean text (paragraph-ID prefix stripped)
    equation_context: dict[str, str] = field(default_factory=dict)
    claim_num: int | None = None
    translation: str | None = None  # filled by translate_* nodes
    # Deterministic preservation of '[001]'-style paragraph IDs: when the head
    # paragraph starts with such a marker, chunk_body strips it from ``text``
    # (so the LLM never sees it and can't mangle it) and stashes it here so
    # translate_body prepends it back verbatim onto the translation.
    paragraph_id_prefix: str | None = None
    # Set True when the chunk's translation has already been applied directly
    # to the source paragraphs (per-paragraph in-place writer for equation-
    # bearing chunks). The write node sees this flag and skips the chunk so
    # it doesn't double-apply or run the chunk-level distribution logic.
    applied_in_place: bool = False

    # Claim-specific structural fields, populated for kind == "claim".
    # Filled by chunk_claims via the deterministic claim_classifier.
    claim_kind: ClaimKind | None = None
    is_independent: bool | None = None
    parent_claim_nums: list[int] = field(default_factory=list)
    multi_parent_kind: MultiParent = "single"
    # Filled after the LLM translates the parent independent claim;
    # consumed when its dependents are translated in phase 2.
    noun_phrase: str | None = None
    actor_phrase: str | None = None
    independent_preamble: str | None = None
    korean_subject_span: str | None = None


class TranslationState(TypedDict, total=False):
    """LangGraph state — flows through every node."""
    # inputs
    input_path: Path
    output_path: Path
    font: str
    font_size: int | float
    review: bool
    verbose: bool
    delay: float
    progress: Callable[[str], None]
    client: LLMClient

    # extracted
    doc: Any                       # python-docx Document
    records: list[ParagraphRecord]

    # produced by chunking nodes
    chunks_body: list[Chunk]
    chunks_abstract: list[Chunk]
    chunks_claims: list[Chunk]

    # cross-section consistency
    glossary: dict[str, str]       # Korean term → English term

    # diagnostics
    started_at: float
    # OMML signatures captured at load() time so write() can report whether
    # any <m:oMath> element got lost / moved / duplicated by the pipeline.
    math_snapshot: list
