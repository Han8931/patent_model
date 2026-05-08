"""Unit test for the 'symbols listed first, then descriptions' repair path.

Reproduces the exact failure mode the user reported with the LLR / 〖BIT〗_3k
patent claim. Calls the parser directly so we can see what comes out without
running the full graph.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from translate.agent.nodes.write import (  # noqa: E402
    _split_into_parameter_clauses,
    _repair_list_then_descs,
)


CASES = [
    # The user's actual broken LLM output (truncated as shown in their message).
    "wherein\nLLR θ_k 〖BIT〗_3k 〖BIT〗_(3k+1) 〖BIT〗_(3k+2) μ\n"
    "is the bit reliability data;  is the phase-difference value of the k-th "
    "phase-difference data; k is an integer",

    # Cleaner well-formed clause list (should hit pass 1).
    "wherein LLR is the bit reliability data; "
    "θ_k is the phase-difference value of the k-th phase-difference data; "
    "〖BIT〗_3k is the 3k-th bit; "
    "〖BIT〗_(3k+1) is the (3k+1)-th bit; "
    "〖BIT〗_(3k+2) is the (3k+2)-th bit; "
    "μ is the noise factor",

    # CJK bracket symbol that the OLD regex rejected silently.
    "where 〖BIT〗_3k is the k-th bit and μ is the noise factor",
]

for i, blob in enumerate(CASES, 1):
    print(f"--- case {i} ---")
    print(f"IN : {blob!r}")
    pairs = _split_into_parameter_clauses(blob)
    print(f"OUT ({len(pairs)} pairs):")
    for sym, clause in pairs:
        print(f"  {sym!r:>20s} → {clause!r}")
    print()
