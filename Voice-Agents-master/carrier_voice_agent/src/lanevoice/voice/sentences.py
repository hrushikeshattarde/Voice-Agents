"""Cut a stream of composed text into sentences the voice can start on.

The composer's reply arrives a few characters at a time. The voice should not
wait for the last of them: the moment a sentence is complete it can be checked
against the money guard and handed to the synthesiser, while the model is still
writing the next one. This is the cut. It is deliberately conservative — a
missed boundary costs a slightly later start, a false one puts a pause in the
middle of a name — so it only ends a sentence at . ! ? followed by whitespace
and then a capital, a digit or an opening quote, and never after a decimal, an
initial or a spoken abbreviation ("St. Louis", "10 a.m.").
"""

from __future__ import annotations

import re

# Sentence-final punctuation, an optional closing quote or bracket, then the
# whitespace that separates it from the next sentence's first character.
_BOUNDARY_RE = re.compile(r"""([.!?]+["')\]]?)(\s+)(?=["'(\[]?[A-Z0-9])""")
# The token in front of the punctuation: letters and internal dots ("a.m").
_LAST_TOKEN_RE = re.compile(r"""([A-Za-z][A-Za-z.]*)[.!?]+["')\]]?$""")
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "st", "vs", "a.m", "p.m", "e.g", "i.e", "etc", "inc", "jr", "sr",
})


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """The complete sentences at the front of `buffer`, and the unfinished rest.

    >>> split_sentences("Got it. Load 2532717, that the one? Alright, so")
    (['Got it.', 'Load 2532717, that the one?'], 'Alright, so')
    """
    sentences: list[str] = []
    start = 0
    for match in _BOUNDARY_RE.finditer(buffer):
        candidate = buffer[start:match.end(1)].strip()
        token = _LAST_TOKEN_RE.search(candidate)
        if token is not None:
            word = token.group(1).rstrip(".").lower()
            if word in _ABBREVIATIONS or len(word) == 1:
                continue                     # "St. Louis", "J. Smith": not an end
        if candidate:
            sentences.append(candidate)
        start = match.end()
    return sentences, buffer[start:]
