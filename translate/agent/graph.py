"""Build the LangGraph state machine for sequential translation."""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from .nodes.chunk_abstract import chunk_abstract
from .nodes.chunk_body import chunk_body
from .nodes.chunk_claims import chunk_claims
from .nodes.classify import classify
from .nodes.compare import compare_translations
from .nodes.load import load
from .nodes.plan_claim_preambles import plan_claim_preambles
from .nodes.review import make_decide, make_revise, needs_revision
from .nodes.static import apply_static
from .nodes.translate_abstract import translate_abstract
from .nodes.translate_body import translate_body
from .nodes.translate_claims import translate_claims
from .nodes.write import write
from .state import TranslationState


def build_graph():
    """Compile the linear translation graph.

    Linear flow (sequential by design — keeps glossary state coherent):
        load → classify → apply_static
             → chunk_claims → plan_claim_preambles → translate_claims → review_claims
             → chunk_body → translate_body → review_body
             → chunk_abstract → translate_abstract → review_abstract
             → compare_translations → write
    """
    g = StateGraph(TranslationState)

    g.add_node("load", load)
    g.add_node("classify", classify)
    g.add_node("apply_static", apply_static)

    g.add_node("chunk_body", chunk_body)
    g.add_node("translate_body", translate_body)
    g.add_node("review_decide_body", make_decide("body"))
    g.add_node("review_revise_body", make_revise("body"))

    g.add_node("chunk_abstract", chunk_abstract)
    g.add_node("translate_abstract", translate_abstract)
    g.add_node("review_decide_abstract", make_decide("abstract"))
    g.add_node("review_revise_abstract", make_revise("abstract"))

    g.add_node("chunk_claims", chunk_claims)
    g.add_node("plan_claim_preambles", plan_claim_preambles)
    g.add_node("translate_claims", translate_claims)
    g.add_node("review_decide_claims", make_decide("claims"))
    g.add_node("review_revise_claims", make_revise("claims"))

    g.add_node("compare_translations", compare_translations)
    g.add_node("write", write)

    g.set_entry_point("load")

    g.add_edge("load", "classify")
    g.add_edge("classify", "apply_static")

    g.add_edge("apply_static", "chunk_claims")
    g.add_edge("chunk_claims", "plan_claim_preambles")
    g.add_edge("plan_claim_preambles", "translate_claims")
    g.add_edge("translate_claims", "review_decide_claims")
    g.add_conditional_edges(
        "review_decide_claims",
        needs_revision("claims"),
        {"revise": "review_revise_claims", "skip": "chunk_body"},
    )
    g.add_edge("review_revise_claims", "chunk_body")

    g.add_edge("chunk_body", "translate_body")
    g.add_edge("translate_body", "review_decide_body")
    g.add_conditional_edges(
        "review_decide_body",
        needs_revision("body"),
        {"revise": "review_revise_body", "skip": "chunk_abstract"},
    )
    g.add_edge("review_revise_body", "chunk_abstract")

    g.add_edge("chunk_abstract", "translate_abstract")
    g.add_edge("translate_abstract", "review_decide_abstract")
    g.add_conditional_edges(
        "review_decide_abstract",
        needs_revision("abstract"),
        {"revise": "review_revise_abstract", "skip": "compare_translations"},
    )
    g.add_edge("review_revise_abstract", "compare_translations")

    g.add_edge("compare_translations", "write")
    g.add_edge("write", END)

    return g.compile()
