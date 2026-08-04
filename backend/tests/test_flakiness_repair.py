"""Tests for repair generation and the Algorithm 1 validation loop.

No Docker and no LLM: FakeDockerBuilder plus scripted fake models.
"""

import json

import pytest

from dockerfile_generation.build import BuildResult, FakeDockerBuilder
from flakiness_repair.detector import detect
from flakiness_repair.embed import LexicalEmbedder
from flakiness_repair.knowledge import KnowledgeBase, RetrievalHit
from flakiness_repair.loop import (
    FAILURE_THRESHOLD,
    UNABLE_TO_RESOLVE,
    repair_flakiness,
)
from flakiness_repair.repair import (
    FalseRepairRecord,
    RepairCandidate,
    generate_repair,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BROKEN = "FROM debian:bullseye\nRUN wget -q https://zlib.net/zlib.tar.gz\n"
_FIXED = "FROM debian:bullseye\nRUN wget -q https://zlib.net/fossils/zlib.tar.gz\n"

_FAIL_LOG = """\
#5 [2/2] RUN wget -q https://zlib.net/zlib.tar.gz
#5 0.4 wget: server returned error: 404 Not Found
#5 ERROR: process "/bin/sh -c wget -q https://zlib.net/zlib.tar.gz" did not complete successfully: exit code: 8
------
ERROR: failed to solve: process "/bin/sh -c wget -q https://zlib.net/zlib.tar.gz" did not complete successfully: exit code: 8
"""

_OTHER_FAIL_LOG = """\
#7 [2/2] RUN apt-get install -y libfoo
#7 1.2 E: Unable to locate package libfoo
#7 ERROR: process "/bin/sh -c apt-get install -y libfoo" did not complete successfully: exit code: 100
------
ERROR: failed to solve: process "/bin/sh -c apt-get install -y libfoo" did not complete successfully: exit code: 100
"""

_OK = BuildResult(success=True, exit_code=0, log="Successfully built", image_id="ok")


def _fail(log=_FAIL_LOG):
    return BuildResult(success=False, exit_code=1, log=log)


class _ScriptedLLM:
    """Returns queued responses in order; records the prompts it received."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.prompts.append(user)
        if not self._responses:
            return f"```dockerfile\n{_FIXED}```"
        return self._responses.pop(0)


class _TemperatureLLM:
    """Accepts a temperature, so the paper's setting can be applied."""

    def __init__(self) -> None:
        self.temperatures: list[float | None] = []

    def complete(self, system: str, user: str, temperature: float | None = None) -> str:
        self.temperatures.append(temperature)
        return f"```dockerfile\n{_FIXED}```"


def _report(*results):
    """Build a FlakinessReport by running detect over scripted build results."""
    return detect(_BROKEN, "/repo", FakeDockerBuilder(list(results)),
                  iterations=len(results))


# ---------------------------------------------------------------------------
# generate_repair — response parsing
# ---------------------------------------------------------------------------

def test_extracts_dockerfile_from_a_fenced_block():
    llm = _ScriptedLLM(f"Here is the problem…\n\n```dockerfile\n{_FIXED}```")
    result = generate_repair(_BROKEN, "404 not found", llm)
    assert result.dockerfile.startswith("FROM debian")
    assert "fossils" in result.dockerfile


def test_prefers_the_last_dockerfile_block_after_reasoning():
    """Chain-of-thought often quotes the broken file before giving the fix."""
    llm = _ScriptedLLM(
        f"The original was:\n```dockerfile\n{_BROKEN}```\n"
        f"Repaired:\n```dockerfile\n{_FIXED}```"
    )
    assert "fossils" in generate_repair(_BROKEN, "err", llm).dockerfile


def test_accepts_an_unfenced_dockerfile():
    llm = _ScriptedLLM(_FIXED)
    assert generate_repair(_BROKEN, "err", llm).dockerfile.startswith("FROM")


def test_ignores_a_non_dockerfile_code_block():
    llm = _ScriptedLLM(
        "First run this:\n```bash\napt-get update\n```\n"
        f"Then:\n```dockerfile\n{_FIXED}```"
    )
    assert generate_repair(_BROKEN, "err", llm).dockerfile.startswith("FROM")


def test_prose_only_response_raises():
    llm = _ScriptedLLM("I cannot repair this Dockerfile.")
    with pytest.raises(RuntimeError, match="did not return a Dockerfile"):
        generate_repair(_BROKEN, "err", llm)


# ---------------------------------------------------------------------------
# generate_repair — prompt assembly
# ---------------------------------------------------------------------------

def test_prompt_carries_the_dockerfile_and_error():
    llm = _ScriptedLLM()
    generate_repair(_BROKEN, "wget 404 not found", llm)
    prompt = llm.prompts[0]
    assert "zlib.tar.gz" in prompt
    assert "wget 404 not found" in prompt


def test_demonstrations_appear_in_the_prompt(tmp_path):
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(json.dumps({
        "id": "x@y#1", "label": "Broken Link",
        "dockerfile": "FROM debian", "error": "wget 404 zlib",
        "repair_diffs": ["-RUN wget old\n+RUN wget https://zlib.net/fossils/zlib"],
    }), encoding="utf-8")
    kb = KnowledgeBase.load(corpus)
    kb.index(LexicalEmbedder(), cache_path=None)
    hits = kb.retrieve("wget 404 zlib", LexicalEmbedder(), k=1)

    llm = _ScriptedLLM()
    result = generate_repair(_BROKEN, "wget 404", llm, demonstrations=hits)

    prompt = llm.prompts[0]
    assert "Broken Link" in prompt
    assert "fossils" in prompt
    assert result.demonstration_ids == ["x@y#1"]


def test_feedback_appears_in_the_prompt():
    llm = _ScriptedLLM()
    generate_repair(
        _BROKEN, "wget 404", llm,
        feedback=[FalseRepairRecord(dockerfile="FROM debian\nRUN wget bad",
                                    error="still 404")],
    )
    prompt = llm.prompts[0]
    assert "did not work" in prompt.lower()
    assert "still 404" in prompt


def test_no_demonstrations_or_feedback_leaves_prompt_clean():
    llm = _ScriptedLLM()
    generate_repair(_BROKEN, "err", llm)
    prompt = llm.prompts[0]
    assert "Similar failures" not in prompt
    assert "did not work" not in prompt


def test_temperature_zero_is_used_when_the_client_supports_it():
    llm = _TemperatureLLM()
    generate_repair(_BROKEN, "err", llm)
    assert llm.temperatures == [0.0]


def test_client_without_temperature_still_works():
    llm = _ScriptedLLM()
    assert generate_repair(_BROKEN, "err", llm).dockerfile


# ---------------------------------------------------------------------------
# repair_flakiness — Algorithm 1
# ---------------------------------------------------------------------------

def test_repair_accepted_when_validation_builds_all_pass():
    report = _report(_fail(), _fail())
    builder = FakeDockerBuilder([_OK])          # validation builds succeed
    outcome = repair_flakiness(
        _BROKEN, "/repo", report, builder, _ScriptedLLM(), iterations=2,
    )

    assert outcome.success is True
    assert "fossils" in outcome.dockerfile
    assert outcome.validated_builds == 2
    assert outcome.attempt_count == 1


def test_validation_builds_exactly_n_times():
    report = _report(_fail())
    builder = FakeDockerBuilder([_OK])
    repair_flakiness(_BROKEN, "/repo", report, builder, _ScriptedLLM(), iterations=3)
    assert len(builder.calls) == 3


def test_a_partially_failing_validation_is_rejected():
    """One clean build is not enough — flakiness needs every build to pass."""
    report = _report(_fail())
    builder = FakeDockerBuilder([_OK, _fail()])
    outcome = repair_flakiness(
        _BROKEN, "/repo", report, builder, _ScriptedLLM(), iterations=2,
    )
    assert outcome.success is False


def test_gives_up_when_the_same_error_recurs():
    report = _report(_fail())
    builder = FakeDockerBuilder([_fail()])       # every validation fails identically
    outcome = repair_flakiness(
        _BROKEN, "/repo", report, builder, _ScriptedLLM(), iterations=1,
        max_attempts=FAILURE_THRESHOLD,
    )

    assert outcome.success is False
    assert UNABLE_TO_RESOLVE in outcome.message
    assert outcome.attempt_count < FAILURE_THRESHOLD + 1


def test_failed_attempt_is_fed_back_into_the_next_prompt():
    report = _report(_fail())
    builder = FakeDockerBuilder([_fail(_OTHER_FAIL_LOG), _OK])
    llm = _ScriptedLLM(
        f"```dockerfile\n{_BROKEN}```",     # first try still fails
        f"```dockerfile\n{_FIXED}```",      # second succeeds
    )
    outcome = repair_flakiness(
        _BROKEN, "/repo", report, builder, llm, iterations=1,
    )

    assert outcome.success is True
    assert len(llm.prompts) == 2
    assert "did not work" in llm.prompts[1].lower()
    assert "libfoo" in llm.prompts[1]


def test_no_failure_to_repair_is_reported_clearly():
    stable = _report(_OK, _OK)
    outcome = repair_flakiness(
        _BROKEN, "/repo", stable, FakeDockerBuilder([_OK]), _ScriptedLLM(),
    )
    assert outcome.success is False
    assert "Nothing to repair" in outcome.message


def test_generation_failure_is_reported_not_raised():
    report = _report(_fail())
    llm = _ScriptedLLM("no dockerfile here at all")
    outcome = repair_flakiness(
        _BROKEN, "/repo", report, FakeDockerBuilder([_OK]), llm, iterations=1,
    )
    assert outcome.success is False
    assert "Repair generation failed" in outcome.message


def test_runs_without_a_knowledge_base():
    report = _report(_fail())
    outcome = repair_flakiness(
        _BROKEN, "/repo", report, FakeDockerBuilder([_OK]), _ScriptedLLM(),
        knowledge=None, embedder=None, iterations=1,
    )
    assert outcome.success is True


def test_progress_reports_generation_and_validation():
    seen: list[str] = []
    report = _report(_fail())
    repair_flakiness(
        _BROKEN, "/repo", report, FakeDockerBuilder([_OK]), _ScriptedLLM(),
        iterations=1, progress=lambda s, m: seen.append(m),
    )
    assert any("Generating repair" in m for m in seen)
    assert any("Validating repair" in m for m in seen)


def test_attempts_record_which_demonstrations_were_used(tmp_path):
    corpus = tmp_path / "c.jsonl"
    corpus.write_text(json.dumps({
        "id": "demo#1", "label": "Broken Link",
        "dockerfile": "FROM debian", "error": "wget 404 zlib not found",
        "repair_diffs": ["+RUN wget fossils"],
    }), encoding="utf-8")
    kb = KnowledgeBase.load(corpus)
    kb.index(LexicalEmbedder(), cache_path=None)

    report = _report(_fail())
    outcome = repair_flakiness(
        _BROKEN, "/repo", report, FakeDockerBuilder([_OK]), _ScriptedLLM(),
        knowledge=kb, embedder=LexicalEmbedder(), iterations=1,
    )
    assert outcome.attempts[0].demonstration_ids == ["demo#1"]
