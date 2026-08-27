"""Scoring: the judge's normalization and the metrics no model may invent.

The judge's LLM is scripted through the same JudgeChat contract the provider
path satisfies, so what's on trial is everything AROUND the model: the lenient
parse, the clamping, the code-computed overall, the retry, and the rule that a
dead judge degrades to a stored `judge_error` — never a crashed end-of-session
request. The metrics tests are pure arithmetic: those numbers are the part of
the report a manager can trust week over week, so they must never drift.
"""

from __future__ import annotations

import json

from lanevoice.db.database import Database
from lanevoice.practice.judge import FOCUS_KEY, RUBRIC, score_session
from lanevoice.practice.metrics import compute_metrics
from lanevoice.practice.profiles import load_profiles
from lanevoice.practice.sessions import PracticeSessionManager
from lanevoice.practice.store import PracticeStore
from lanevoice.settings import get_settings

PROFILE = load_profiles()["brush_off"]

TRANSCRIPT = [
    ["customer", "Prairie Steel shipping, this is Dale."],
    ["rep", "Hey Dale, um, quick question — who covers your overflow? "
            "And, uh, what happens at quarter end?"],
    ["customer", "We've got a broker for that. Why?"],
    ["rep", "Because you know, when he's slow after five, we're not."],
]


def _verdict(**overrides) -> str:
    scores = {k: {"score": 6, "quote": "quick question", "comment": "solid"}
              for k in [*RUBRIC, FOCUS_KEY]}
    body = {"scores": scores, "win_condition_met": False,
            "win_evidence": "no commitment was made",
            "strengths": ["asked a real question"],
            "improvements": [{"what": "cut the fillers", "why": "reads as nerves",
                              "quote": "um, quick question",
                              "better_line": "Dale — one question and I'm gone."}],
            "summary": "Decent discovery, no close."}
    body.update(overrides)
    return json.dumps(body)


def _chat(*replies):
    calls = {"n": 0}

    def chat(system: str, user: str, *, max_tokens: int) -> str:
        calls["n"] += 1
        assert calls["n"] <= len(replies), "the judge was called more than scripted"
        return replies[calls["n"] - 1]

    chat.calls = calls
    return chat


# ------------------------------------------------------------------ metrics #
def test_voice_metrics_are_time_based_and_exact():
    m = compute_metrics(TRANSCRIPT, "voice", rep_audio_secs=30.0,
                        customer_audio_secs=10.0, duration_secs=65.4,
                        end_reason="hangup")
    assert m["talk_ratio"] == 0.75            # 30 / (30 + 10), by TIME
    assert m["questions"] == 2                # two question marks in rep turns
    assert m["fillers"] == 3                  # um, uh, you know
    assert m["fillers_per_min"] == 6.0        # 3 fillers over half a minute
    assert m["wpm"] == 54                     # 27 rep tokens over half a minute
    assert m["hung_up_on"] is True
    assert m["duration_secs"] == 65


def test_text_metrics_fall_back_to_words_and_skip_pace():
    m = compute_metrics(TRANSCRIPT, "text", rep_audio_secs=0.0,
                        customer_audio_secs=0.0, duration_secs=120.0,
                        end_reason="ended")
    rep, customer = 27, 13
    assert m["talk_ratio"] == round(rep / (rep + customer), 2)
    # A made-up pace would be worse than none: no seconds, no WPM.
    assert m["wpm"] is None
    assert m["fillers_per_min"] is None
    assert m["fillers"] == 3                  # the count still stands
    assert m["hung_up_on"] is False


def test_an_empty_transcript_produces_nulls_not_a_crash():
    m = compute_metrics([], "text", 0.0, 0.0, 5.0, "abandoned")
    assert m["talk_ratio"] is None
    assert m["rep_turns"] == 0


# -------------------------------------------------------------------- judge #
def test_a_clean_verdict_is_normalised_with_a_code_computed_overall():
    report = score_session(PROFILE, TRANSCRIPT, _chat(_verdict()), get_settings())
    assert set(report["scores"]) == {*RUBRIC, FOCUS_KEY}
    assert report["overall"] == 6.0           # mean taken HERE, not trusted
    assert report["win_condition_met"] is False
    assert report["improvements"][0]["better_line"].startswith("Dale")


def test_scores_outside_the_scale_are_clamped_and_junk_is_dropped():
    scores = {k: {"score": 6, "quote": "", "comment": ""} for k in [*RUBRIC, FOCUS_KEY]}
    scores["opening"]["score"] = 15           # model enthusiasm
    scores["closing"]["score"] = -3           # model despair
    scores["value"]["score"] = "great"        # model nonsense
    report = score_session(PROFILE, TRANSCRIPT, _chat(_verdict(scores=scores)),
                           get_settings())
    assert report["scores"]["opening"]["score"] == 10
    assert report["scores"]["closing"]["score"] == 0
    assert report["scores"]["value"]["score"] is None
    # The junk dimension is excluded from the mean instead of poisoning it.
    assert report["overall"] == round((10 + 0 + 6 * 5) / 7, 1)


def test_a_verdict_wrapped_in_prose_still_parses():
    chatty = "Here is my assessment:\n" + _verdict() + "\nHope this helps!"
    report = score_session(PROFILE, TRANSCRIPT, _chat(chatty), get_settings())
    assert report["overall"] == 6.0


def test_an_unparseable_judge_is_retried_once_then_stored_as_an_error():
    chat = _chat("I would rate this call quite highly overall.",
                 "Still just prose, no JSON.")
    report = score_session(PROFILE, TRANSCRIPT, chat, get_settings())
    assert chat.calls["n"] == 2               # exactly one retry
    assert "judge_error" in report
    assert "raw" in report                    # the evidence survives for debugging


def test_the_judge_prompt_carries_the_answer_key_and_the_transcript():
    """The judge — unlike the browser — DOES get the hidden facts: it can't
    grade discovery without knowing what there was to discover."""
    seen = {}

    def capture(system: str, user: str, *, max_tokens: int) -> str:
        seen["user"] = user
        return _verdict()

    score_session(PROFILE, TRANSCRIPT, capture, get_settings())
    for fact in PROFILE.hidden_facts:
        assert fact in seen["user"]
    assert PROFILE.win_condition in seen["user"]
    assert "REP: Hey Dale" in seen["user"]


# ------------------------------------------------------------------ manager #
def _script(*lines):
    queue = list(lines)

    def chat(system: str, user: str, *, max_tokens: int) -> str:
        assert queue, "the customer script ran out of lines"
        return queue.pop(0)

    return chat


def _manager(tmp_path, customer_lines, judge_replies):
    db = Database(tmp_path / "practice.db")
    db.reset(seed=False)
    mgr = PracticeSessionManager(
        db, get_settings(),
        chat_factory=lambda s: _script(*customer_lines),
        judge_factory=lambda s: _chat(*judge_replies))
    return mgr, PracticeStore(db)


def test_ending_a_session_scores_it_and_stores_the_report(tmp_path):
    mgr, store = _manager(tmp_path, ["We're covered."], [_verdict()])
    started = mgr.start("brush_off", "Jordan")
    mgr.turn(started["session_id"], "Who covers your overflow?")
    summary = mgr.end(started["session_id"])
    assert summary["report"]["overall"] == 6.0
    assert summary["report"]["metrics"]["questions"] == 1

    detail = store.report_detail(started["session_id"])
    assert detail["report"]["overall"] == 6.0
    assert detail["report"]["scores"]["opening"]["score"] == 6
    assert detail["report"]["metrics"]["mode"] == "text"


def test_a_customer_hangup_scores_inside_the_final_turn(tmp_path):
    mgr, store = _manager(tmp_path, ["Not interested. [HANGUP]"], [_verdict()])
    started = mgr.start("brush_off", "Jordan")
    res = mgr.turn(started["session_id"], "Hi Dale —")
    assert res["done"] is True
    assert res["summary"]["report"]["overall"] == 6.0
    assert store.report_detail(started["session_id"])["report"] is not None


def test_a_zero_turn_end_and_an_abandon_are_never_scored(tmp_path):
    mgr, store = _manager(tmp_path, [], [])   # judge script empty: a call is a bug
    started = mgr.start("brush_off", "Jordan")
    summary = mgr.end(started["session_id"])
    assert "report" not in summary
    assert store.report_detail(started["session_id"])["report"] is None

    started = mgr.start("brush_off", "Jordan")
    mgr.abandon(started["session_id"])
    assert store.report_detail(started["session_id"])["report"] is None


def test_a_dead_judge_lands_as_a_stored_error_not_a_crash(tmp_path):
    def dead_judge(settings):
        raise RuntimeError("the judge model is down")

    db = Database(tmp_path / "practice.db")
    db.reset(seed=False)
    mgr = PracticeSessionManager(db, get_settings(),
                                 chat_factory=lambda s: _script("We're covered."),
                                 judge_factory=dead_judge)
    started = mgr.start("brush_off", "Jordan")
    mgr.turn(started["session_id"], "Quick question —")
    summary = mgr.end(started["session_id"])          # must not raise
    assert "the judge model is down" in summary["report"]["judge_error"]
    # The metrics don't need a model, so they land even when the judge dies.
    assert summary["report"]["metrics"]["rep_turns"] == 1
    detail = PracticeStore(db).report_detail(started["session_id"])
    assert detail["report"]["judge_error"]


def test_the_reports_list_reads_newest_first_with_headlines(tmp_path):
    # One verdict per finished session, in finishing order — the factories build
    # a fresh chat per session, so the pool has to live outside them.
    verdicts = [_verdict(), _verdict(win_condition_met=True)]
    db = Database(tmp_path / "practice.db")
    db.reset(seed=False)
    mgr = PracticeSessionManager(
        db, get_settings(),
        chat_factory=lambda s: _script("We're covered."),
        judge_factory=lambda s: _chat(verdicts.pop(0)))
    store = PracticeStore(db)

    first = mgr.start("brush_off", "Jordan")
    mgr.turn(first["session_id"], "Who covers overflow?")
    mgr.end(first["session_id"])
    second = mgr.start("rate_shopper", "Priya")
    mgr.turn(second["session_id"], "It's not about the rate —")
    mgr.end(second["session_id"])

    rows = store.reports()
    assert [r["profile_id"] for r in rows] == ["rate_shopper", "brush_off"]
    assert rows[0]["win_condition_met"] is True
    assert rows[0]["overall"] == 6.0
    assert store.reports(rep="Jord")[0]["rep_name"] == "Jordan"
    assert store.reports(rep="nobody") == []
