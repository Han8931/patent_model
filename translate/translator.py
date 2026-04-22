"""Paragraph-level Korean→English patent translator using an OpenAI-compatible API."""

import re
import time
from collections import deque
from pathlib import Path

from docx import Document

from .client import LLMClient, ClientConfig
from .prompt import build_messages

# Section headers translated by lookup, not LLM
SECTION_HEADER_MAP = {
    "[발명의 설명]": "DESCRIPTION",
    "[발명의 명칭]": "TITLE OF INVENTION",
    "[도면의 간단한 설명]": "BRIEF DESCRIPTION OF THE DRAWINGS",
    "[발명의 실시를 위한 구체적인 내용]": "DETAILED DESCRIPTION OF EMBODIMENTS",
    "[특허청구범위]": "CLAIMS",
    "[요약서]": "ABSTRACT",
    "[발명의 효과]": "ADVANTAGEOUS EFFECTS OF INVENTION",
    "[기술적 과제]": "TECHNICAL PROBLEM",
    "[과제의 해결 수단]": "SOLUTION TO PROBLEM",
}

_BLANK_RE = re.compile(r'^\s*$')


def _break_after_semicolons(text: str) -> str:
    """Insert a newline after each semicolon that is followed by a space."""
    return re.sub(r';\s+', ';\n', text)


class PatentTranslator:
    def __init__(self, config: ClientConfig | None = None, context_window: int = 3):
        """
        Args:
            config: LLM client configuration.
            context_window: Number of preceding (korean, english) paragraph pairs
                            to include in each request for terminology consistency.
                            Set to 0 to disable.
        """
        self.config = config or ClientConfig()
        self.client = LLMClient(self.config)
        self.context_window = context_window

    def _translate_paragraph(
        self, text: str, context: list[tuple[str, str]]
    ) -> str:
        mapped = SECTION_HEADER_MAP.get(text.strip())
        if mapped:
            return mapped

        messages = build_messages(text, context=context)
        result = self.client.complete(messages).strip()
        return _break_after_semicolons(result)

    def translate_document(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        delay: float = 0.5,
        verbose: bool = True,
    ) -> None:
        input_path = Path(input_path)
        output_path = Path(output_path)

        doc = Document(input_path)
        out_doc = Document()

        # Rolling buffer of (korean, english) pairs for context injection
        history: deque[tuple[str, str]] = deque(maxlen=self.context_window)

        for i, para in enumerate(doc.paragraphs):
            raw = para.text

            if _BLANK_RE.match(raw):
                out_doc.add_paragraph()
                continue

            # Static header — add to output but don't pollute the context buffer
            if SECTION_HEADER_MAP.get(raw.strip()):
                translated = SECTION_HEADER_MAP[raw.strip()]
                out_para = out_doc.add_paragraph(translated)
                out_para.style = para.style
                if verbose:
                    print(f"[{i:03d}] HEADER → {translated}")
                continue

            if verbose:
                preview = raw[:60].replace('\n', ' ')
                print(f"[{i:03d}] Translating: {preview}…")

            translated = self._translate_paragraph(raw, list(history))
            history.append((raw, translated))

            out_para = out_doc.add_paragraph(translated)
            out_para.style = para.style

            if delay > 0:
                time.sleep(delay)

        out_doc.save(output_path)
        if verbose:
            print(f"\nSaved → {output_path}")
