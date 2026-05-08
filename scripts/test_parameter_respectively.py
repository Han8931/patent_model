"""Offline checks for expanding collapsed parameter legends.

Run:
    uv run python scripts/test_parameter_respectively.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from translate.agent.docx_utils import postprocess  # noqa: E402


def _assert_postprocess(source: str, expected: str) -> None:
    actual = postprocess(source)
    if actual != expected:
        raise AssertionError(
            f"\nsource  : {source!r}\nexpected: {expected!r}\nactual  : {actual!r}"
        )


def main() -> None:
    _assert_postprocess(
        "α, β, and γ are X, Y, and Z, respectively.",
        "α is X;\nβ is Y;\nγ is Z.",
    )
    _assert_postprocess(
        "alpha, beta, and gamma mean a width, a height, and a depth, respectively.",
        "alpha means a width;\nbeta means a height;\ngamma means a depth.",
    )
    _assert_postprocess(
        "α, β, and γ respectively denote a first value, a second value, and a third value.",
        "α denotes a first value;\nβ denotes a second value;\nγ denotes a third value.",
    )
    _assert_postprocess(
        "x, y, and z correspond to a row, a column, and a layer, respectively.",
        "x corresponds to a row;\ny corresponds to a column;\nz corresponds to a layer.",
    )
    _assert_postprocess(
        "where alpha, beta, and gamma is a first value; is a second value; is a third value.",
        "where alpha is a first value;\nbeta is a second value;\ngamma is a third value.",
    )
    _assert_postprocess(
        "wherein α, β, and γ is a first value; wherein is a second value; wherein is a third value.",
        "wherein α is a first value;\nβ is a second value;\nγ is a third value.",
    )
    print("parameter respectively expansion: ok")


if __name__ == "__main__":
    main()
