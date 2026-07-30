"""Unit tests for select.py — all 4 selection rules, no LLM calls needed for rules 1-3."""

import pytest

from dockerfile_generation.select import select_patch

# ---------------------------------------------------------------------------
# Minimal fake LLM — only invoked by rule 4
# ---------------------------------------------------------------------------

class _FakeLLM:
    """Returns a configurable digit string so rule-4 tests can control the pick."""

    def __init__(self, reply: str = "1") -> None:
        self.reply = reply
        self.call_count = 0

    def complete(self, system: str, user: str) -> str:
        self.call_count += 1
        return self.reply


# ── Rule 1: prefer untried ─────────────────────────────────────────────────

def test_rule1_picks_first_untried():
    tried = {"patch_a", "patch_b"}
    candidates = ["patch_a", "patch_b", "patch_c"]
    assert select_patch(candidates, tried, "some error", _FakeLLM()) == "patch_c"


def test_rule1_picks_first_in_list_among_multiple_untried():
    tried = {"patch_a"}
    candidates = ["patch_b", "patch_c", "patch_a"]
    assert select_patch(candidates, tried, "some error", _FakeLLM()) == "patch_b"


def test_rule1_does_not_call_llm():
    tried: set[str] = set()
    candidates = ["patch_x", "patch_y", "patch_z"]
    llm = _FakeLLM()
    select_patch(candidates, tried, "error", llm)
    assert llm.call_count == 0


# ── Rule 2: consensus ─────────────────────────────────────────────────────

def test_rule2_consensus_when_all_tried():
    consensus = "FROM python:3.11\nRUN pip install fastapi"
    other = "FROM ubuntu:22.04\nRUN apt install python3"
    candidates = [consensus, other, consensus]  # consensus appears twice
    tried = {consensus, other}
    result = select_patch(candidates, tried, "error", _FakeLLM())
    assert result == consensus


def test_rule2_all_three_identical():
    patch = "FROM python:3.11"
    candidates = [patch, patch, patch]
    tried = {patch}
    result = select_patch(candidates, tried, "error", _FakeLLM())
    assert result == patch


def test_rule2_does_not_call_llm():
    consensus = "FROM python:3.11"
    other = "FROM ubuntu"
    candidates = [consensus, other, consensus]
    tried = {consensus, other}
    llm = _FakeLLM()
    select_patch(candidates, tried, "error", llm)
    assert llm.call_count == 0


# ── Rule 3: keyword match ─────────────────────────────────────────────────

_DOCKERFILE_WITH_COPY = (
    "FROM python:3.11\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt"
)
_DOCKERFILE_NO_COPY = "FROM python:3.11\nWORKDIR /app\nRUN pip install fastapi"
_DOCKERFILE_UNRELATED = "FROM ubuntu:22.04\nRUN apt-get install curl"


def test_rule3_keyword_match_picks_relevant_candidate():
    candidates = [_DOCKERFILE_NO_COPY, _DOCKERFILE_WITH_COPY, _DOCKERFILE_UNRELATED]
    tried = set(candidates)  # all tried → skip rule 1 and 2 (all unique)
    error = "No such file or directory: 'requirements.txt'"
    result = select_patch(candidates, tried, error, _FakeLLM())
    assert result == _DOCKERFILE_WITH_COPY


def test_rule3_does_not_call_llm_when_match_found():
    candidates = [_DOCKERFILE_NO_COPY, _DOCKERFILE_WITH_COPY, _DOCKERFILE_UNRELATED]
    tried = set(candidates)
    llm = _FakeLLM()
    select_patch(candidates, tried, "requirements.txt not found", llm)
    assert llm.call_count == 0


# ── Rule 4: LLM chooses ───────────────────────────────────────────────────

def test_rule4_llm_called_when_no_other_rule_applies():
    """All tried, no consensus, error words not in any candidate → rule 4."""
    candidates = ["patch_x", "patch_y", "patch_z"]  # all different, no keyword overlap
    tried = set(candidates)
    error = "build failed"  # generic — not mentioned in any candidate
    llm = _FakeLLM(reply="2")
    result = select_patch(candidates, tried, error, llm)
    assert llm.call_count == 1
    assert result == "patch_y"  # 1-indexed "2" → index 1


def test_rule4_llm_picks_candidate_3():
    candidates = ["alpha", "beta", "gamma"]
    tried = set(candidates)
    llm = _FakeLLM(reply="3")
    result = select_patch(candidates, tried, "build failed", llm)
    assert result == "gamma"


def test_rule4_clamps_out_of_range_index():
    candidates = ["alpha", "beta", "gamma"]
    tried = set(candidates)
    llm = _FakeLLM(reply="9")  # out of range → clamped to last
    result = select_patch(candidates, tried, "build failed", llm)
    assert result in candidates


def test_rule4_handles_non_digit_reply():
    candidates = ["alpha", "beta"]
    tried = set(candidates)
    llm = _FakeLLM(reply="I think option one is best.")
    result = select_patch(candidates, tried, "build failed", llm)
    # Should parse the first digit "1" → index 0
    assert result == "alpha"


# ── Edge cases ────────────────────────────────────────────────────────────

def test_single_candidate_untried_returns_immediately():
    result = select_patch(["only_patch"], set(), "error", _FakeLLM())
    assert result == "only_patch"


def test_empty_candidates_raises():
    with pytest.raises(ValueError):
        select_patch([], set(), "error", _FakeLLM())
