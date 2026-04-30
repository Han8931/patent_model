"""Static node — apply non-translation transforms (section headers, claim numbers, image fonts)."""

from __future__ import annotations

from ..docx_utils import replace_text
from ..state import TranslationState


def apply_static(state: TranslationState) -> dict:
    records = state["records"]
    font = state["font"]
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)

    for r in records:
        if r.kind == "section_header":
            replace_text(r.para, r.mapped or "", font)
            progress(f"→ {r.mapped}")
            if verbose:
                print(f"HEADER → {r.mapped}")
        elif r.kind == "claim_header":
            # Leave Korean text intact — the write node will overwrite this paragraph
            # with the translated "N. <body>". Apply font only.
            for run in r.para.runs:
                run.font.name = font
        elif r.kind == "image":
            for run in r.para.runs:
                run.font.name = font
            if verbose:
                print("PRESERVED (image/equation)")

    return {}
