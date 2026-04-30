"""Load node — copy input docx to output path and open it."""

from __future__ import annotations

import shutil
import time

from docx import Document

from ..state import TranslationState


def load(state: TranslationState) -> dict:
    input_path = state["input_path"]
    output_path = state["output_path"]
    progress = state.get("progress") or (lambda _: None)

    progress(f"Loading {input_path.name}…")
    shutil.copy2(input_path, output_path)
    doc = Document(output_path)

    return {
        "doc": doc,
        "started_at": time.time(),
        "glossary": {},
    }
