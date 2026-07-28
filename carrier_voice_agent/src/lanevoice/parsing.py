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


# Words Whisper hallucinates on silence/noise (esp. "Thank you.", "you", "so").
# A turn made up ENTIRELY of these, with no digits, is almost certainly a phantom.
_NOISE_WORDS = {
    "thank", "thanks", "you", "bye", "goodbye", "so", "please", "subscribe",
    "for", "watching", "um", "uh", "ah", "mm", "hmm", "oh", "the", "a", "well",
}


def is_probably_noise(text: str) -> bool:
    """True if the transcript looks like a Whisper silence-hallucination."""
    if re.search(r"\d", text):          # any digit -> real content (load/MC/rate)
        return False
    words = re.findall(r"[a-z]+", text.lower())
    return not words or all(w in _NOISE_WORDS for w in words)


_TLDS = "com|net|org|io|co|us|biz|info|trucking|transport"


def _spoken_email(text: str) -> str | None:
    """Recover an address a human SAID rather than typed.

    Speech-to-text hands us "dispatch at blueskylogistics dot com" — or worse,
    "dispatch at blue sky logistics dot com" with the domain split into words.
    Only attempted when both an 'at' and a 'dot' are present, so ordinary
    sentences don't get mangled into addresses.
    """
    low = f" {text.lower().strip()} "
    if not re.search(r"\bdot\b", low) or not re.search(r"\bat\b", low):
        return None
    low = re.sub(r"\b(?:underscore|under score)\b", "_", low)
    low = re.sub(r"\b(?:dash|hyphen|minus)\b", "-", low)
    low = re.sub(r"\b(?:dot|period|point)\b", ".", low)
    low = re.sub(r"\bat\b", "@", low)
    low = re.sub(r"\s*([@._-])\s*", r"\1", low)       # tighten around separators
    # The domain may still be spoken as separate words — allow spaces up to the TLD.
    match = re.search(rf"([\w.+-]+)@([\w\s-]+?\.(?:{_TLDS}))\b", low)
    if not match:
        return None
    return f"{match.group(1)}@{match.group(2)}".replace(" ", "")


def extract_email(text: str) -> str | None:
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    if match:
        return match.group().rstrip(".")
    return _spoken_email(text)


def extract_phone(text: str) -> str | None:
    """A 10-digit US phone (kept separate from 6-digit MC / 4-digit rates)."""
    match = re.search(
        r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text
    )
    return match.group().strip() if match else None


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
