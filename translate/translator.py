"""Paragraph-level Korean→English patent translator using an OpenAI-compatible API."""

import re
import time
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from .client import LLMClient, ClientConfig
from .prompt import build_messages

# Section headers that should be translated but not sent to the LLM
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

# Paragraphs that are pure whitespace or empty
_BLANK_RE = re.compile(r'^\s*$')


class PatentTranslator:
    def __init__(self, config: ClientConfig | None = None):
        self.config = config or ClientConfig()
        self.client = LLMClient(self.config)

    def _translate_paragraph(self, text: str) -> str:
        """Translate a single paragraph of Korean patent text."""
        if SECTION_HEADER_MAP.get(text.strip()):
            return SECTION_HEADER_MAP[text.strip()]

        messages = build_messages(text)
        return self.client.complete(messages).strip()

    def translate_document(
        self,
        input_path: str | Path,
        output_path: str | Path,
        *,
        delay: float = 0.5,
        verbose: bool = True,
    ) -> None:
        """
        Read a cleaned Korean patent docx, translate each paragraph,
        and write an English docx preserving the document structure.
        """
        input_path = Path(input_path)
        output_path = Path(output_path)

        doc = Document(input_path)
        out_doc = Document()

        # Copy core document styles from source so formatting is retained
        for i, para in enumerate(doc.paragraphs):
            raw = para.text

            if _BLANK_RE.match(raw):
                out_doc.add_paragraph()
                continue

            # Check for static header mapping first
            mapped = SECTION_HEADER_MAP.get(raw.strip())
            if mapped:
                out_para = out_doc.add_paragraph(mapped)
                out_para.style = para.style
                if verbose:
                    print(f"[{i:03d}] HEADER → {mapped}")
                continue

            # Translate via LLM
            if verbose:
                preview = raw[:60].replace('\n', ' ')
                print(f"[{i:03d}] Translating: {preview}…")

            translated = self._translate_paragraph(raw)

            out_para = out_doc.add_paragraph(translated)
            out_para.style = para.style

            if delay > 0:
                time.sleep(delay)

        out_doc.save(output_path)
        if verbose:
            print(f"\nSaved translated document → {output_path}")
