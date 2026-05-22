"""Static node — apply non-translation transforms (section headers, claim numbers, image fonts)."""

from __future__ import annotations

from ..docx_utils import replace_text, set_run_font
from ..state import TranslationState


def apply_static(state: TranslationState) -> dict:
    records = state["records"]
    font = state["font"]
    font_size = state.get("font_size", 12)
    progress = state.get("progress") or (lambda _: None)
    verbose = state.get("verbose", False)

    for r in records:
        if r.kind == "section_header":
            replace_text(r.para, r.mapped or "", font)
        elif r.kind == "claim_header":
            # Leave Korean text intact — the write node will overwrite this paragraph
            # with the translated "N. <body>". Apply font only.
            for run in r.para.runs:
                set_run_font(run, font, font_size)
        elif r.kind == "image":
            for run in r.para.runs:
                set_run_font(run, font, font_size)
            if verbose:
                print("PRESERVED (image/equation)")

    return {}
