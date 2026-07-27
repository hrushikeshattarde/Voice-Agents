"""
Utterance parsing — extract structured entities (load IDs, MC/DOT numbers,
dollar amounts) from a carrier's spoken text. Regex-first: cheap and reliable,
and unit-testable in isolation.
"""

from __future__ import annotations

import re


def extract_load_id(text: str) -> str | None:
    """Match 'L1001', 'load 1001', 'L 10 01' -> 'L1001'."""
    compact = text.upper().replace(" ", "")
    match = re.search(r"L?\d{4,6}", compact)
    if not match:
        return None
    token = match.group()
    digits = re.sub(r"\D", "", token)
    return token if token.startswith("L") else f"L{digits}"


def extract_mc_dot(text: str) -> tuple[str | None, str | None]:
    """
    Return ('MC'|'DOT', number) or (None, None).

    Classifies by the label nearest the number, tolerating filler words
    ("my MC is 123456", "MC number 123456") and glued forms ("MC123456").
    Defaults to DOT (the primary identifier per PRD §8.3) when unlabeled.
    """
    upper = text.upper()
    num = re.search(r"\d{4,8}", upper)
    if not num:
        return None, None
    number = num.group()

    window = upper[max(0, num.start() - 25):num.start()]
    _dot = r"US ?DOT|\bDOT\b"
    if re.search(r"\bMC\b", window) and not re.search(_dot, window):
        return "MC", number
    if re.search(_dot, window):
        return "DOT", number
    # No label next to the number — fall back to the whole utterance.
    if re.search(r"\bMC\b", upper) and not re.search(_dot, upper):
        return "MC", number
    return "DOT", number


def extract_money(text: str) -> float | None:
    """Extract a dollar amount: '$2,100', '2100', '2.1k'."""
    lowered = text.lower().replace(",", "")
    match = re.search(r"\$?\s*(\d{3,6})(?:\s*(?:dollars|bucks))?", lowered)
    if match:
        return float(match.group(1))
    kilo = re.search(r"(\d+(?:\.\d+)?)\s*k", lowered)
    if kilo:
        return float(kilo.group(1)) * 1000
    return None
