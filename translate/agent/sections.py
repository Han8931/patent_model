"""Section header detection — Korean → English label map and claim-number regex."""

import re

SECTION_HEADER_MAP = {
    "[발명의 설명]":                    "DESCRIPTION",
    "[발명의 명칭]":                    "TITLE OF INVENTION",
    "[도면의 간단한 설명]":              "BRIEF DESCRIPTION OF THE DRAWINGS",
    "[발명의 실시를 위한 구체적인 내용]": "DETAILED DESCRIPTION OF EMBODIMENTS",
    "[특허청구범위]":                   "CLAIMS",
    "[청구범위]":                       "CLAIMS",
    "【특허청구범위】":                  "CLAIMS",
    "【청구범위】":                      "CLAIMS",
    "특허청구범위":                      "CLAIMS",
    "청구범위":                          "CLAIMS",
    "[요약서]":                         "ABSTRACT",
    "【요약서】":                        "ABSTRACT",
    "요약서":                            "ABSTRACT",
    "[발명의 효과]":                    "ADVANTAGEOUS EFFECTS OF INVENTION",
    "[기술적 과제]":                    "TECHNICAL PROBLEM",
    "[과제의 해결 수단]":               "SOLUTION TO PROBLEM",
    "[대표도]":                         "REPRESENTATIVE FIGURE",
    "대표도":                           "REPRESENTATIVE FIGURE",
}

# Korean claim header: 【청구항 N】 or [청구항 N] (with optional spaces).
# No $ — also matches when body text follows on the same paragraph.
CLAIM_HEADER_RE = re.compile(r'^[【\[]\s*청구항\s*(\d+)\s*[】\]]\s*')

# Any leading claim-number prefix the LLM might produce.
LLM_CLAIM_PREFIX_RE = re.compile(r'^(?:CLAIMS?\s+)?(\d+)[.:\s]\s*', re.IGNORECASE)

# Lowercase first letter after :\n or ;\n in claim text.
CLAIM_ELEMENT_CAP_RE = re.compile(r'(?<=[;:])\n([A-Z])')

BLANK_RE = re.compile(r'^\s*$')


# Section labels that route to the BODY translator
BODY_SECTIONS = {
    "DESCRIPTION",
    "TITLE OF INVENTION",
    "BRIEF DESCRIPTION OF THE DRAWINGS",
    "DETAILED DESCRIPTION OF EMBODIMENTS",
    "TECHNICAL FIELD",
    "BACKGROUND ART",
    "TECHNICAL PROBLEM",
    "SOLUTION TO PROBLEM",
    "ADVANTAGEOUS EFFECTS OF INVENTION",
    "REPRESENTATIVE FIGURE",
}
