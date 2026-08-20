"""Token estimation without a tokenizer dependency.

An exact count would mean pulling in a model-specific tokenizer, which is a
heavy dependency for a number that only needs to be good enough to enforce a
budget. The estimate below is deliberately conservative: it counts CJK
characters at roughly one token each and Latin text at roughly one token per
four characters, which errs toward over-counting and therefore toward
returning less than the cap rather than more.
"""

from __future__ import annotations

# CJK ranges that tokenizers generally split at roughly one token per character.
_CJK_RANGES = (
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFF66, 0xFF9F),  # Halfwidth Katakana
    (0xAC00, 0xD7AF),  # Hangul
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """Approximate the token cost of ``text``.

    Returns at least 1 for any non-empty string so that a budget loop always
    makes progress.
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for char in text:
        if _is_cjk(char):
            cjk += 1
        else:
            other += 1
    return max(1, cjk + (other + 3) // 4)
