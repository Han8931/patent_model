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

from ..state import TranslationState


def load(state: TranslationState) -> dict:
    input_path = state["input_path"]
    output_path = state["output_path"]
    progress = state.get("progress") or (lambda _: None)

    progress(f"Loading {input_path.name}…")
    doc = Document(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    return {
        "doc": doc,
        "started_at": time.time(),
        "glossary": {},
    }
