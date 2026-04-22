import re
import docx
from docx import Document
from copy import deepcopy
from pathlib import Path


NUMBERING_PATTERN = re.compile(r'^\[\d{4}\]')


def strip_paragraph_numbering(text: str) -> str:
    return NUMBERING_PATTERN.sub('', text).lstrip()


def process_document(input_path: str, output_path: str) -> None:
    doc = Document(input_path)

    for para in doc.paragraphs:
        full_text = para.text
        if not NUMBERING_PATTERN.match(full_text):
            continue

        # Find which run contains the numbering tag and strip it
        tag_end = NUMBERING_PATTERN.match(full_text).end()
        chars_to_remove = tag_end

        # Strip leading space after tag
        if chars_to_remove < len(full_text) and full_text[chars_to_remove] == ' ':
            chars_to_remove += 1

        for run in para.runs:
            if chars_to_remove <= 0:
                break
            run_len = len(run.text)
            if chars_to_remove >= run_len:
                run.text = ''
                chars_to_remove -= run_len
            else:
                run.text = run.text[chars_to_remove:]
                chars_to_remove = 0

    doc.save(output_path)
    print(f"Saved: {output_path}")


if __name__ == '__main__':
    data_dir = Path('data')
    process_document(
        str(data_dir / 'published1_kr_processed.docx'),
        str(data_dir / 'published1_kr_clean.docx'),
    )
    process_document(
        str(data_dir / 'published1_en_processed.docx'),
        str(data_dir / 'published1_en_clean.docx'),
    )
