"""Load node — open the input docx in-memory.

Important: we do NOT pre-copy the input to the output path. Doing so used to
hand users a Korean doc that *looked* successfully translated whenever any
later node raised an exception (the file existed, but contained the original
text). The write node creates ``output_path`` only after every chunk has been
applied, so a partial run leaves no output file at all — failures are loud,
not silent.
"""

from __future__ import annotations

import time

from docx import Document

from ..docx_utils import snapshot_math_locations
from ..state import TranslationState


def load(state: TranslationState) -> dict:
    input_path = state["input_path"]
    output_path = state["output_path"]
    progress = state.get("progress") or (lambda _: None)

    progress(f"Loading {input_path.name}…")
    doc = Document(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Equation integrity baseline — taken before any node touches the doc, so
    # the write-end check can report exactly where each math element moved /
    # whether any was lost or duplicated.
    math_snapshot = snapshot_math_locations(doc)
    progress(f"Loaded — {len(math_snapshot)} math element(s) detected for integrity tracking")

    return {
        "doc": doc,
        "started_at": time.time(),
        "glossary": {},
        "math_snapshot": math_snapshot,
    }
