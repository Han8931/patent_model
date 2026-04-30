"""Patent translator — thin wrapper around the LangGraph agentic pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .agent.graph import build_graph
from .agent.state import TranslationState
from .client import ClientConfig, LLMClient


class PatentTranslator:
    def __init__(
        self,
        config: ClientConfig | None = None,
        batch_size: int = 30,  # accepted for backward compatibility; agent uses per-section chunking
    ):
        self.config = config or ClientConfig()
        self.client = LLMClient(self.config)
        self._graph = build_graph()

    def translate_document(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        font: str = "Times New Roman",
        delay: float = 0.5,
        verbose: bool = True,
        review: bool = True,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        progress = progress_callback or (lambda _: None)

        initial: TranslationState = {
            "input_path": Path(input_path),
            "output_path": Path(output_path),
            "font": font,
            "review": review,
            "verbose": verbose,
            "delay": delay,
            "progress": progress,
            "client": self.client,
        }

        # Invoke the compiled graph; LangGraph threads state through every node.
        # Increase recursion_limit so deeply-nested conditional edges don't trip.
        self._graph.invoke(initial, config={"recursion_limit": 50})
