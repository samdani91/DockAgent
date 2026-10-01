"""Integration coverage for Module 4 and resilience — real pipeline, real model.

Covers report cases T13-T15.
"""

import os

import pytest

from coordination.explain import answer_query, explain, summarise
from coordination.orchestrator import Modules, PipelineRequest, run_pipeline
from coordination.state import FailingTest, PipelineState, TestOutcome
from dockerfile_generation.build import RealDockerBuilder

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# T13 — Conversational assistance
# ---------------------------------------------------------------------------

def _state_with_failures() -> PipelineState:
    state = PipelineState(workspace_path="/w", dockerfile_path="/w/Dockerfile")
    state.test = TestOutcome(
        executed=True, total=10, passed=8, failed=2,
        failing=[
            FailingTest("check node version", ["expected v20.20.*, got v18.19.0"]),
            FailingTest("/app/index.js exists", ["file does not exist"]),
        ],
        message="8 of 10 container tests passed.",
    )
    return state


@pytest.mark.needs_llm
@pytest.mark.slow
def test_t13_assistant_explains_a_real_build_error(llm):
    """A question about a failure is answered from the run, in plain language."""
    answer = answer_query("why did the tests fail?", _state_with_failures(), llm)

    assert len(answer) > 40
    # It must name the specifics rather than restate the log.
    assert "v18" in answer or "18.19" in answer
    assert "index.js" in answer
    assert "```" not in answer, "returned a code block instead of prose"


@pytest.mark.needs_llm
@pytest.mark.slow
def test_t13_assistant_says_so_when_there_is_no_run(llm):
    answer = answer_query("why did my build fail?", None, llm)
    assert len(answer) > 30
    assert any(w in answer.lower() for w in ("no ", "not ", "haven't", "has not"))


@pytest.mark.needs_llm
@pytest.mark.slow
def test_t13_failures_are_explained_without_being_asked(llm):
    """The agent volunteers an explanation plus a recommendation."""
    state = _state_with_failures()
    note = explain("test", state.test.message, state, llm)

    assert note.explanation and len(note.explanation) > 40
    assert note.recommendation, "no next step suggested"
    assert isinstance(note.action_required, bool)


@pytest.mark.needs_llm
@pytest.mark.slow
def test_t13_run_summary_is_written_in_prose(llm):
    summary = summarise(_state_with_failures(), llm)
    assert len(summary) > 40
    assert "{" not in summary, "leaked JSON into the summary"


# ---------------------------------------------------------------------------
# T14 — One-click pipeline and the unified feedback loop
# ---------------------------------------------------------------------------

@pytest.mark.needs_llm
@pytest.mark.slow
def test_t14_pipeline_generates_then_tests_then_checks_flakiness(user_service):
    """A project with no Dockerfile, driven end to end by the agent."""
    assert not (user_service / "Dockerfile").exists()

    events: list[tuple[str, str, str]] = []
    state = run_pipeline(
        PipelineRequest(
            workspace_path=str(user_service),
            threshold=12,          # focused suite, or S5 runs for ages
            iterations=1,
            max_attempts=4,
        ),
        lambda stage, step, message: events.append((stage, step, message)),
    )

    assert state.stage == "done"
    assert state.generation is not None and state.generation.success, \
        f"generation failed: {state.generation.message if state.generation else '—'}"
    assert (user_service / "Dockerfile").is_file(), "no Dockerfile was written"

    assert state.test is not None, "tests never ran"
    assert state.flakiness is not None, "flakiness never ran"

    # Modules must have run in the documented order.
    order = [r.stage for r in state.history if r.stage != "agent"]
    assert order.index("generate") < order.index("test") < order.index("flakiness")

    # The agent narrates its own decisions.
    assert any(stage == "agent" for stage, _, _ in events)


@pytest.mark.slow
def test_t14_failing_tests_are_routed_back_for_repair(user_service):
    """Feedback A: a test failure must drive a repair, not end the run.

    The modules are substituted so a failure can be staged deterministically;
    the routing, the instruction built from the failures, and the round
    accounting are the real implementation under test.
    """
    from coordination.state import FlakinessOutcome, GenerationOutcome

    calls = {"test": 0, "patch": 0}
    instructions: list[str] = []

    def generate(request, state, emit):
        return GenerationOutcome(success=True, attempts=1, message="generated")

    def test(request, state, emit):
        calls["test"] += 1
        if calls["test"] == 1:                     # first run fails
            return TestOutcome(
                executed=True, total=10, passed=8, failed=2,
                failing=[FailingTest("check node version",
                                     ["expected v20.20.*, got v18.19.0"])],
                message="8 of 10 passed.",
            )
        return TestOutcome(executed=True, total=10, passed=10, failed=0,
                           message="10 of 10 passed.")

    def patch(request, state, instruction, emit):
        calls["patch"] += 1
        instructions.append(instruction)
        return GenerationOutcome(success=True, attempts=1, message="patched")

    def flakiness(request, state, emit):
        return FlakinessOutcome(verdict="stable", message="No instability observed.")

    (user_service / "Dockerfile").write_text("FROM node:20-alpine\nCMD [\"node\"]\n")

    events: list[tuple[str, str, str]] = []
    state = run_pipeline(
        PipelineRequest(workspace_path=str(user_service)),
        lambda s, st, m: events.append((s, st, m)),
        Modules(generate=generate, test=test, flakiness=flakiness, patch=patch, llm=None),
    )

    assert calls["patch"] == 1, "the failure was not routed back"
    assert calls["test"] == 2, "tests were not re-run after the repair"
    assert state.feedback_rounds == 1

    # The instruction must carry the actual failing test.
    assert "check node version" in instructions[0]
    assert "v20.20" in instructions[0]

    # The pipeline continued rather than terminating on the failure.
    assert state.flakiness is not None
    assert state.stage == "done"

    routed = [m for s, _, m in events if s == "agent"]
    assert any("routing back" in m.lower() for m in routed)


# ---------------------------------------------------------------------------
# T15 — External service failure handling
# ---------------------------------------------------------------------------

def test_t15_missing_docker_is_reported_clearly(undocumented_project, monkeypatch):
    """A build with no docker on PATH must report, not crash."""
    monkeypatch.setenv("PATH", "/nonexistent")
    builder = RealDockerBuilder(timeout=30)

    result = builder.build("FROM alpine:3.19\n", str(undocumented_project))

    assert result.success is False
    assert "docker" in result.log.lower()
    assert "not found" in result.log.lower()


def test_t15_missing_docker_is_reported_by_the_test_runner(tmp_path, monkeypatch):
    from test_generation.executor import execute_tests

    spec = tmp_path / "spec.yaml"
    spec.write_text("schemaVersion: '2.0.0'\n")
    monkeypatch.setenv("PATH", "/nonexistent")

    with pytest.raises(RuntimeError, match="(?i)docker.*not found"):
        execute_tests("some-image", str(spec), timeout=30)


@pytest.mark.slow
def test_t15_an_invalid_api_key_fails_with_a_clear_message(monkeypatch):
    """A bad key must surface as an error, not a crash or a silent hang."""
    monkeypatch.setenv("GEMINI_API_KEY", "definitely-not-a-valid-key")
    from dockerfile_generation.llm import GeminiClient

    with pytest.raises(Exception) as exc:
        GeminiClient(model="gemini-flash-latest").complete("Be terse.", "Say OK")

    message = str(exc.value).lower()
    assert any(w in message for w in ("api key", "unauthenticated", "invalid", "400", "401", "403"))


def test_t15_a_missing_key_is_reported_before_any_work(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from coordination.runners import _require_llm

    with pytest.raises(ValueError, match="API_KEY is not set"):
        _require_llm(PipelineRequest(workspace_path="/w"))


@pytest.mark.needs_llm
@pytest.mark.slow
def test_t15_the_assistant_degrades_rather_than_crashing(monkeypatch):
    """A failing provider must not take the chat panel down."""
    monkeypatch.setenv("GEMINI_API_KEY", "definitely-not-a-valid-key")
    from dockerfile_generation.llm import GeminiClient

    answer = answer_query("why did it fail?", None, GeminiClient(model="gemini-flash-latest"))
    assert isinstance(answer, str) and answer, "returned nothing at all"
    assert "could not" in answer.lower() or "error" in answer.lower()
