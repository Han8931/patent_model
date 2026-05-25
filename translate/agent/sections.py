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
    "[기술분야]":                      "TECHNICAL FIELD",
    "【기술분야】":                     "TECHNICAL FIELD",
    "기술분야":                         "TECHNICAL FIELD",
    "[배경기술]":                      "BACKGROUND ART",
    "【배경기술】":                     "BACKGROUND ART",
    "배경기술":                         "BACKGROUND ART",
    "[발명의 배경이 되는 기술]":        "BACKGROUND ART",
    "[기술적 과제]":                    "TECHNICAL PROBLEM",
    "[과제의 해결 수단]":               "SOLUTION TO PROBLEM",
    "[대표도]":                         "REPRESENTATIVE FIGURE",
    "대표도":                           "REPRESENTATIVE FIGURE",
}


# ---------------------------------------------------------------------------
# Hangul-signature lookup — fallback when the exact-match map misses.
# ---------------------------------------------------------------------------
# Some patents wrap headers with extra decoration (a leading bullet "•",
# spacing inside brackets "[ 청구범위 ]", different bracket forms (), 〈〉,
# trailing English ("[청구범위] (Claims)"), etc. The exact-match dict can't
# cover every variant, so we also derive a Hangul-only signature from the
# stripped paragraph text and look it up here. A length cap stops ordinary
# sentences that happen to contain "청구범위" from being mis-tagged as headers.

_HEADER_BY_SIG: dict[str, str] = {}


def _hangul_signature(text: str) -> str:
    """All Hangul syllables in ``text``, no whitespace, no punctuation."""
    return "".join(c for c in text if "가" <= c <= "힣")


for _phrase, _label in SECTION_HEADER_MAP.items():
    _sig = _hangul_signature(_phrase)
    if _sig:
        _HEADER_BY_SIG.setdefault(_sig, _label)


# Heuristic length cap: real headers are short (a few words wrapped in
# brackets, optional trailing English in parens). Anything longer is almost
# certainly a regular sentence that shouldn't be re-classified.
_HEADER_MAX_LEN = 60


def detect_section_header(stripped: str) -> str | None:
    """Robust header detection.

    Order:
      1. exact-match the cleaned text against ``SECTION_HEADER_MAP``,
      2. fall back to a Hangul-signature lookup (length-capped) so that
         '• [청구범위]', '[ 청구범위 ]', '[청구범위] (Claims)' all resolve to
         CLAIMS while ordinary sentences containing the word do not.
    """
    if not stripped:
        return None
    if stripped in SECTION_HEADER_MAP:
        return SECTION_HEADER_MAP[stripped]
    if len(stripped) > _HEADER_MAX_LEN:
        return None
    sig = _hangul_signature(stripped)
    return _HEADER_BY_SIG.get(sig)

# Korean claim header variants.  No $ — also matches when body text follows on
# the same paragraph.  Covers common forms such as:
#   【청구항 1】, [청구항 제1항], 청구항 1., 청구항 제1항,
#   제1항., 1., 1)
CLAIM_HEADER_RE = re.compile(
    r'^\s*(?:[【\[]\s*)?(?:'
    r'청구항\s*(?:제\s*)?(\d+)\s*(?:항)?|'
    r'제\s*(\d+)\s*항|'
    r'(\d+)\s*[\.)]'
    r')\s*(?:[】\]]\s*)?(?:[:：.．\)\]]\s*)?'
)

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
