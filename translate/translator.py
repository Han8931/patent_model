"""Patent translator — thin wrapper around the LangGraph agentic pipeline."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import traceback
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
        font_size: int | float = 12,
        delay: float = 0.0,
        verbose: bool = True,
        review: bool = True,
        log_path: str | Path | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        # Default progress sink: print to stdout. `verbose` controls extra
        # per-chunk detail separately; the high-level step messages always print
        # unless the caller passes an explicit no-op callback.
        input_path = Path(input_path)
        output_path = Path(output_path)
        resolved_log_path = (
            Path(log_path)
            if log_path is not None
            else output_path.with_suffix(".log")
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_log_path.parent.mkdir(parents=True, exist_ok=True)

        progress_sink = progress_callback if progress_callback is not None else print

        def write_log(message: str) -> None:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                with resolved_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"[{timestamp}] {message}\n")
            except OSError:
                # Logging must never become the reason a translation fails.
                pass

        try:
            with resolved_log_path.open("w", encoding="utf-8") as fh:
                started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                fh.write(f"[{started}] Translation log started\n")
                fh.write(f"input={input_path}\n")
                fh.write(f"output={output_path}\n")
                fh.write(f"model={self.config.model}\n")
                fh.write(f"base_url={self.config.base_url}\n")
                fh.write(f"review={review}\n")
                fh.write("\n")
        except OSError:
            pass

        def progress(message: str) -> None:
            progress_sink(message)
            write_log(message)

        initial: TranslationState = {
            "input_path": input_path,
            "output_path": output_path,
            "font": font,
            "font_size": font_size,
            "review": review,
            "verbose": verbose,
            "delay": delay,
            "progress": progress,
            "client": self.client,
        }

        # Invoke the compiled graph; LangGraph threads state through every node.
        # Increase recursion_limit so deeply-nested conditional edges don't trip.
        try:
            self._graph.invoke(initial, config={"recursion_limit": 50})
            write_log("Translation completed successfully")
        except Exception as exc:
            write_log(f"Translation failed: {type(exc).__name__}: {exc}")
            for line in traceback.format_exc().rstrip().splitlines():
                write_log(line)
            raise
