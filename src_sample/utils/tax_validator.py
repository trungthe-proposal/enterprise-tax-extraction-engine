"""
tax_validator.py
================

Reference implementation: clipboard sanitization and format validation for tax
identifiers.

This module is a *reference sample* that illustrates the general pattern
(normalize -> tokenize -> validate). It is intentionally minimal and is not a
copy of any production code.

Supported canonical formats (all values below are fictitious examples):

    10 digits         Ex: '0123456789'
    12 digits         Ex: '012345678901'
    Branch (10-3)     Ex: '0123456789-001'
"""

from __future__ import annotations

import re
from typing import List

# ``[0-9]`` instead of ``\d`` so that non-ASCII digits are never accepted, and
# ``fullmatch`` so that trailing characters (including a newline) can never slip
# through the way they can with ``$``.
_TAX_ID_PATTERN = re.compile(r"[0-9]{10}|[0-9]{12}|[0-9]{10}-[0-9]{3}")

# Zero-width / byte-order-mark characters that commonly sneak in from
# spreadsheets and web pages, plus the non-breaking space.
_INVISIBLE_CHARS = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
_SEPARATORS = re.compile(r"[\s,;]+")


def validate_tax_id_format(tax_id: str) -> bool:
    """Return ``True`` if ``tax_id`` matches one of the canonical formats.

    The check is strict: it performs no trimming and no auto-correction, so
    callers should sanitize input first (see :func:`parse_excel_clipboard`).

    Valid examples:
        >>> validate_tax_id_format('0123456789')         # Ex: 10 digits
        True
        >>> validate_tax_id_format('012345678901')       # Ex: 12 digits
        True
        >>> validate_tax_id_format('0123456789-001')     # Ex: branch, 10-3
        True

    Invalid examples:
        >>> validate_tax_id_format('123456789')          # too short
        False
        >>> validate_tax_id_format('0123456789-01')      # malformed branch suffix
        False
        >>> validate_tax_id_format(' 0123456789')        # not sanitized
        False
        >>> validate_tax_id_format(None)                 # never raises
        False
    """
    if not isinstance(tax_id, str):
        return False
    return _TAX_ID_PATTERN.fullmatch(tax_id) is not None


def parse_excel_clipboard(raw_text: str) -> List[str]:
    """Turn text pasted from a spreadsheet into a clean list of tokens.

    Handles the three shapes a spreadsheet copy can take:

    * a column      -> values separated by newlines
    * a row         -> values separated by tabs
    * free typing   -> commas, semicolons or spaces

    Empty cells and surrounding whitespace are dropped; order and duplicates
    are preserved so that the caller decides how to treat them. Tokens are
    returned as-is (this function does *not* validate them).

        >>> parse_excel_clipboard('0123456789\\r\\n0123456789-001\\r\\n')
        ['0123456789', '0123456789-001']
        >>> parse_excel_clipboard('0123456789\\t\\t012345678901')
        ['0123456789', '012345678901']
        >>> parse_excel_clipboard('')
        []
    """
    if not raw_text:
        return []
    cleaned = raw_text.translate(_INVISIBLE_CHARS)
    return [token for token in _SEPARATORS.split(cleaned) if token]


if __name__ == "__main__":
    pasted = "0123456789\n012345678901\t0123456789-001\n123456789\n"
    for token in parse_excel_clipboard(pasted):
        print(f"{token:<16} valid={validate_tax_id_format(token)}")
