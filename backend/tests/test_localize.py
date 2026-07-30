"""Unit tests for localize.py — keyword scan only (no LLM required)."""

import pytest

from dockerfile_generation.localize import keyword_localize


def _make_log(*lines: str) -> str:
    return "\n".join(lines)


# ── keyword_localize ────────────────────────────────────────────────────────


def test_finds_no_such_file():
    log = _make_log(
        "Step 1/3 : FROM python:3.11",
        "Step 2/3 : RUN pip install -r requirements.txt",
        "ERROR: Could not open requirements file: [Errno 2] No such file or directory: 'requirements.txt'",
    )
    result = keyword_localize(log)
    assert result is not None
    assert "no such file or directory" in result.lower()


def test_finds_unable_to_locate_package():
    log = _make_log(
        "Step 1/2 : FROM ubuntu:22.04",
        "Step 2/2 : RUN apt-get install -y libfoo",
        "E: Unable to locate package libfoo",
    )
    result = keyword_localize(log)
    assert result is not None
    assert "unable to locate package" in result.lower()


def test_finds_no_matching_distribution():
    log = _make_log(
        "Step 1/2 : FROM python:3.11",
        "Step 2/2 : RUN pip install foo==999",
        "ERROR: Could not find a version that satisfies the requirement foo==999",
        "ERROR: No matching distribution found for foo==999",
    )
    result = keyword_localize(log)
    assert result is not None
    # Last matching line (scanned in reverse) should be the distribution error
    assert "distribution" in result.lower() or "matching" in result.lower()


def test_returns_last_matching_line_in_reverse_scan():
    """Keyword scan goes in reverse — the LAST error line in the log is returned."""
    log = _make_log(
        "ERROR: first error",
        "Step 2/2 : RUN something",
        "ERROR: second error",
    )
    result = keyword_localize(log)
    assert result == "ERROR: second error"


def test_returns_none_when_no_error():
    log = _make_log(
        "Step 1/2 : FROM python:3.11",
        "Step 2/2 : RUN echo hello",
        "Successfully built abc123",
    )
    result = keyword_localize(log)
    assert result is None


def test_case_insensitive():
    log = _make_log("STEP 1: FROM ubuntu", "MISSING dependency xyz")
    result = keyword_localize(log)
    assert result is not None
    assert "MISSING" in result


def test_keyword_precedence_longer_phrase_matched_first():
    """'no matching distribution found for' should be recognised (multi-word phrase)."""
    log = _make_log("No matching distribution found for torch==99")
    result = keyword_localize(log)
    assert result is not None


def test_empty_log_returns_none():
    assert keyword_localize("") is None


def test_whitespace_only_log_returns_none():
    assert keyword_localize("   \n   \n   ") is None
