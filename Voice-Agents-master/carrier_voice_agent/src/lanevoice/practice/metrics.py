"""
Deterministic delivery metrics — the numbers no model gets to make up.

Everything here is computed from the transcript and the recorded talk time, so
two reps with the same call get the same numbers, forever. That's the point:
the judge's rubric scores carry nuance and drift a little between runs; these
don't, so week-over-week progress ("fillers per minute down from 6 to 2") can
be trusted arithmetic rather than model mood.

Voice sessions carry real seconds (the browser's push-to-talk timer for the
rep, exact synthesis lengths for the customer), so the timing metrics — talk
ratio by time, words per minute, fillers per minute — are only computed there.
A text session falls back to a word-count talk ratio and skips pace entirely;
a made-up WPM would be worse than none.
"""

from __future__ import annotations

import re

# Verbal filler, conservatively: only tokens that are ~never legitimate words in
# a sales call. "like" and "so" are real words far too often to count.
_FILLER_RE = re.compile(r"\b(?:um+|uh+|erm+|er|ah+|hmm+|mm+)\b|\byou know\b",
                        re.IGNORECASE)


def compute_metrics(transcript: list[list[str]], mode: str, rep_audio_secs: float,
                    customer_audio_secs: float, duration_secs: float,
                    end_reason: str) -> dict:
    rep_lines = [line for who, line in transcript if who == "rep"]
    customer_lines = [line for who, line in transcript if who == "customer"]
    rep_words = sum(len(line.split()) for line in rep_lines)
    customer_words = sum(len(line.split()) for line in customer_lines)
    fillers = sum(len(_FILLER_RE.findall(line)) for line in rep_lines)

    voice = mode == "voice" and rep_audio_secs > 0
    if voice:
        talk_ratio = rep_audio_secs / (rep_audio_secs + customer_audio_secs) \
            if (rep_audio_secs + customer_audio_secs) > 0 else None
    else:
        talk_ratio = rep_words / (rep_words + customer_words) \
            if (rep_words + customer_words) > 0 else None

    rep_minutes = rep_audio_secs / 60.0
    return {
        "mode": mode,
        "duration_secs": round(duration_secs),
        "rep_turns": len(rep_lines),
        "rep_words": rep_words,
        "customer_words": customer_words,
        # How much of the conversation the rep occupied. High isn't automatically
        # bad (a pitch has to be made), but a rep at 0.8 against a Brush-off
        # steamrolled someone who wanted to leave.
        "talk_ratio": round(talk_ratio, 2) if talk_ratio is not None else None,
        # Question marks in the rep's turns — the crudest possible discovery
        # proxy, kept because it can't be argued with. The judge's `discovery`
        # score carries the quality; this carries the count.
        "questions": sum(line.count("?") for line in rep_lines),
        "fillers": fillers,
        "fillers_per_min": round(fillers / rep_minutes, 1) if voice else None,
        "wpm": round(rep_words / rep_minutes) if voice else None,
        "hung_up_on": end_reason == "hangup",
    }
