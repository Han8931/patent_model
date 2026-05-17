"""Verify LLM markdown labels/prefaces do not leak into translated DOCX text.

Run:
    uv run python scripts/test_markdown_translation_cleanup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from translate.agent.glossary import clean_translation_text  # noqa: E402
from translate.agent.nodes.translate_claims import _minimal_cleanup  # noqa: E402


def main() -> None:
    assert (
        clean_translation_text("**Translation**\n\nSemiconductor package\n\n---")
        == "Semiconductor package"
    )
    assert (
        clean_translation_text(
            "**Abstract (USPTO style)**  \n"
            "A semiconductor package is provided."
        )
        == "A semiconductor package is provided."
    )
    assert (
        clean_translation_text(
            "The first groove may have a first width **W1** and a first depth "
            "**DT1**."
        )
        == "The first groove may have a first width W1 and a first depth DT1."
    )
    assert (
        clean_translation_text(
            "I'm ready to translate the Korean patent passage. Please provide "
            "the Korean text you would like translated."
        )
        == ""
    )

    claim = _minimal_cleanup(
        5,
        "**Translation**\n\n"
        "Claim 5: A semiconductor package comprising: a substrate; and "
        "a first chip on the substrate.",
    )
    assert claim.startswith("5. A semiconductor package comprising:")
    assert "**" not in claim
    assert "Translation" not in claim

    print("markdown translation cleanup: ok")


if __name__ == "__main__":
    main()
