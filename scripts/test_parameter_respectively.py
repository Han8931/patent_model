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
    actual = postprocess(source, claim_format=True)
    if actual != expected:
        raise AssertionError(
            f"\nsource  : {source!r}\nexpected: {expected!r}\nactual  : {actual!r}"
        )


def main() -> None:
    body_actual = postprocess(
        "The controller uses α; β; and γ to calculate an output value."
    )
    body_expected = "The controller uses α; β; and γ to calculate an output value."
    if body_actual != body_expected:
        raise AssertionError(
            f"\nsource  : body semicolon formatting\n"
            f"expected: {body_expected!r}\nactual  : {body_actual!r}"
        )

    _assert_postprocess(
        "α, β, and γ are X, Y, and Z, respectively.",
        "α is X;\n\tβ is Y;\n\tγ is Z.",
    )
    _assert_postprocess(
        "alpha, beta, and gamma mean a width, a height, and a depth, respectively.",
        "alpha means a width;\n\tbeta means a height;\n\tgamma means a depth.",
    )
    _assert_postprocess(
        "α, β, and γ respectively denote a first value, a second value, and a third value.",
        "α denotes a first value;\n\tβ denotes a second value;\n\tγ denotes a third value.",
    )
    _assert_postprocess(
        "x, y, and z correspond to a row, a column, and a layer, respectively.",
        "x corresponds to a row;\n\ty corresponds to a column;\n\tz corresponds to a layer.",
    )
    _assert_postprocess(
        "where alpha, beta, and gamma is a first value; is a second value; is a third value.",
        "where alpha is a first value;\n\tbeta is a second value;\n\tgamma is a third value.",
    )
    _assert_postprocess(
        "wherein α, β, and γ is a first value; wherein is a second value; wherein is a third value.",
        "wherein α is a first value;\n\tβ is a second value;\n\tγ is a third value.",
    )
    print("parameter respectively expansion: ok")


if __name__ == "__main__":
    main()
