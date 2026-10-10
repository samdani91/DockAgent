"""Tests for Module 4 — agentic coordination.

The module entry points are injected, so the whole flow runs with no Docker and
no LLM.
"""

import pytest

from coordination.orchestrator import Modules, PipelineRequest, run_pipeline
from coordination.routing import (
    DONE,
    MAX_FEEDBACK_ROUNDS,
    MODULE_1,
    MODULE_2,
    MODULE_3,
    build_patch_instruction,
    route_flakiness,
    route_test_failure,
    should_stop,
)
from coordination.state import (
    FailingTest,
    FlakinessOutcome,
    GenerationOutcome,
    PipelineState,
    TestOutcome,
)
from test_generation.builder import ImageBuildError

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _passing(total=5):
    return TestOutcome(executed=True, total=total, passed=total, failed=0,
                       message=f"{total} of {total} container tests passed.")


def _failing(names=("check node version",), total=5):
    failing = [FailingTest(name=n, errors=[f"{n} did not hold"]) for n in names]
    return TestOutcome(executed=True, total=total, passed=total - len(failing),
                       failed=len(failing), failing=failing,
                       message=f"{total - len(failing)} of {total} passed.")


def _stable():
    return FlakinessOutcome(verdict="stable", needs_repair=False,
                            message="No instability observed.")


def _repaired(applied=True):
    return FlakinessOutcome(verdict="deterministic-failure", needs_repair=True,
                            repaired=True, applied=applied, attempts=1,
                            repaired_path="/w/.dockagent/Dockerfile.repaired",
                            message="Repaired and validated.")


class _Recorder:
    """Scripted modules that count how often each one ran."""

    def __init__(self, tests, generation=None, flakiness=None, patch_ok=True,
                 repair_ok=True):
        self._tests = list(tests)
        self._generation = generation or GenerationOutcome(
            success=True, attempts=1, message="generated")
        self._flakiness = flakiness or _stable()
        self._patch_ok = patch_ok
        self._repair_ok = repair_ok
        self.calls = {"generate": 0, "test": 0, "flakiness": 0, "patch": 0,
                      "repair_build": 0}
        self.instructions: list[str] = []

    def generate(self, request, state, emit):
        self.calls["generate"] += 1
        return self._generation

    def test(self, request, state, emit):
        self.calls["test"] += 1
        result = self._tests.pop(0) if self._tests else _passing()
        if isinstance(result, Exception):
            raise result
        return result

    def flakiness(self, request, state, emit):
        self.calls["flakiness"] += 1
        return self._flakiness

    def patch(self, request, state, instruction, emit):
        self.calls["patch"] += 1
        self.instructions.append(instruction)
        return GenerationOutcome(
            success=self._patch_ok, attempts=1,
            message="patched" if self._patch_ok else "patch broke the build")

    def repair_build(self, request, state, failure, emit):
        self.calls["repair_build"] += 1
        self.instructions.append(failure.log)
        return GenerationOutcome(
            success=self._repair_ok, attempts=2,
            message="build repaired" if self._repair_ok else "build repair failed")

    def as_modules(self):
        return Modules(generate=self.generate, test=self.test,
                       flakiness=self.flakiness, patch=self.patch, llm=None,
                       repair_build=self.repair_build)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "app.py").write_text("print('hi')")
    return tmp_path


def _run(workspace, recorder, **kw):
    events = []
    request = PipelineRequest(workspace_path=str(workspace), **kw)
    state = run_pipeline(request, lambda s, st, m: events.append((s, st, m)),
                         recorder.as_modules())
    return state, events


# ---------------------------------------------------------------------------
# Module 1 gating
# ---------------------------------------------------------------------------

def test_existing_dockerfile_skips_generation(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_passing()])
    state, _ = _run(workspace, rec)

    assert rec.calls["generate"] == 0
    assert any(r.stage == "generate" and r.outcome == "skipped" for r in state.history)


def test_missing_dockerfile_runs_generation_first(workspace):
    rec = _Recorder([_passing()])
    state, _ = _run(workspace, rec)

    assert rec.calls["generate"] == 1
    stages = [r.stage for r in state.history]
    assert stages.index("generate") < stages.index("test")


def test_failed_generation_stops_the_run(workspace):
    rec = _Recorder([_passing()],
                    generation=GenerationOutcome(success=False, message="no build"))
    state, _ = _run(workspace, rec)

    assert rec.calls["test"] == 0
    assert rec.calls["flakiness"] == 0
    assert state.stage == "done"


# ---------------------------------------------------------------------------
# Feedback A
# ---------------------------------------------------------------------------

def test_passing_tests_need_no_feedback(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_passing()])
    state, _ = _run(workspace, rec)

    assert state.feedback_rounds == 0
    assert rec.calls["patch"] == 0
    assert rec.calls["test"] == 1
    assert rec.calls["flakiness"] == 1


def test_one_failure_then_pass_uses_exactly_one_round(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_failing(), _passing()])
    state, events = _run(workspace, rec)

    assert state.feedback_rounds == 1
    assert rec.calls["patch"] == 1
    assert rec.calls["test"] == 2
    assert rec.calls["flakiness"] == 1
    assert any(stage == "agent" for stage, _, _ in events)


def test_persistent_failure_stops_at_the_cap_but_still_checks_flakiness(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    # Different failures each time, so the signature guard does not fire first.
    rec = _Recorder([_failing(("a",)), _failing(("b",)), _failing(("c",)),
                     _failing(("d",))])
    state, _ = _run(workspace, rec)

    assert state.feedback_rounds == MAX_FEEDBACK_ROUNDS
    assert rec.calls["flakiness"] == 1          # not aborted
    assert state.stage == "done"


def test_identical_failure_twice_stops_early(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_failing(("same",)), _failing(("same",)), _failing(("same",))])
    state, _ = _run(workspace, rec)

    # Stops after the second identical failure rather than using both rounds.
    assert state.feedback_rounds == 1
    assert rec.calls["test"] == 2
    assert rec.calls["flakiness"] == 1


def test_a_patch_that_breaks_the_build_stops_the_loop(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_failing(), _failing()], patch_ok=False)
    state, _ = _run(workspace, rec)

    assert rec.calls["patch"] == 1
    assert rec.calls["test"] == 1               # never retried
    assert rec.calls["flakiness"] == 1


def test_unexecuted_tests_are_not_routed_back(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([TestOutcome(executed=False, message="not run")])
    state, _ = _run(workspace, rec)

    assert state.feedback_rounds == 0
    assert rec.calls["patch"] == 0
    assert rec.calls["flakiness"] == 1


def test_s0_build_failure_repairs_then_retries_tests(workspace):
    (workspace / "Dockerfile").write_text("FROM node:8.9.4\nRUN apt-get update\n")
    failure = ImageBuildError(1, "ERROR: failed to solve: apt-get update returned 100")
    rec = _Recorder([failure, _passing()])
    state, events = _run(workspace, rec)

    assert rec.calls["repair_build"] == 1
    assert rec.calls["patch"] == 0
    assert rec.calls["test"] == 2
    assert rec.calls["flakiness"] == 1
    assert rec.instructions == [failure.log]
    assert state.feedback_rounds == 1
    assert state.test.all_passed
    assert any("before container tests ran" in msg for _, _, msg in events)


# Real buildx output: the solve error is not the last line, and the trailing
# line carries a per-build id.
_BUILDX_LOG = """\
 => ERROR [3/4] RUN npm ci --omit=dev
------
 > [3/4] RUN npm ci --omit=dev:
0.412 npm error The `npm ci` command can only install with an existing lockfile
------
ERROR: failed to build: failed to solve: process "/bin/sh -c npm ci --omit=dev" did not complete successfully: exit code: 1
View build details: docker-desktop://dashboard/build/default/default/qk9x2h7
"""


def test_s0_error_line_is_the_solve_error_not_the_trailing_line(workspace):
    """Regression: a prefix match missed buildx's "failed to build: failed to solve:"."""
    (workspace / "Dockerfile").write_text("FROM node:20-alpine\nRUN npm ci\n")
    rec = _Recorder([ImageBuildError(1, _BUILDX_LOG)], repair_ok=False)
    state, _ = _run(workspace, rec)

    assert "npm ci --omit=dev" in state.test.message
    assert "docker-desktop://" not in state.test.message


def test_repeated_s0_build_error_is_recognised_despite_a_unique_trailing_line(workspace):
    """The repeat guard must key on the error, not on a per-build id."""
    (workspace / "Dockerfile").write_text("FROM node:20-alpine\nRUN npm ci\n")
    second = _BUILDX_LOG.replace("qk9x2h7", "zz41p08")
    rec = _Recorder([ImageBuildError(1, _BUILDX_LOG),
                     ImageBuildError(1, second), _passing()])
    state, _ = _run(workspace, rec, max_feedback_rounds=3)

    assert rec.calls["repair_build"] == 1
    assert rec.calls["test"] == 2
    assert state.feedback_rounds == 1


def test_failed_s0_repair_stops_without_running_flakiness(workspace):
    (workspace / "Dockerfile").write_text("FROM node:8.9.4\n")
    rec = _Recorder([ImageBuildError(1, "ERROR: failed to solve: apt failed")],
                    repair_ok=False)
    state, _ = _run(workspace, rec)

    assert rec.calls["test"] == 1
    assert rec.calls["flakiness"] == 0
    assert not state.generation.success
    assert not state.test.executed
    assert "apt failed" in state.test.message
    assert state.stage == "done"


def test_repeated_s0_build_error_stops_early(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    failure = ImageBuildError(1, "ERROR: failed to solve: same error")
    rec = _Recorder([failure, failure, _passing()])
    state, _ = _run(workspace, rec, max_feedback_rounds=3)

    assert rec.calls["repair_build"] == 1
    assert rec.calls["test"] == 2
    assert rec.calls["flakiness"] == 0
    assert state.feedback_rounds == 1


def test_s0_build_failure_honours_zero_feedback_cap(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([ImageBuildError(1, "ERROR: failed to solve: apt failed")])
    state, _ = _run(workspace, rec, max_feedback_rounds=0)

    assert rec.calls["repair_build"] == 0
    assert rec.calls["flakiness"] == 0
    assert state.stage == "done"


def test_s0_build_repair_shares_cap_with_test_feedback(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_failing(), ImageBuildError(1, "ERROR: failed to solve: build")])
    state, _ = _run(workspace, rec, max_feedback_rounds=1)

    assert rec.calls["patch"] == 1
    assert rec.calls["repair_build"] == 0
    assert rec.calls["flakiness"] == 0
    assert state.feedback_rounds == 1


# ---------------------------------------------------------------------------
# Feedback B
# ---------------------------------------------------------------------------

def test_applied_repair_triggers_one_reverification(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_passing(), _passing()], flakiness=_repaired(applied=True))
    state, _ = _run(workspace, rec)

    assert state.reverified is True
    assert rec.calls["test"] == 2
    assert sum(1 for r in state.history if r.stage == "test") == 2


def test_s0_failure_during_reverification_uses_build_repair(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_passing(), ImageBuildError(1, "ERROR: failed to solve: build"),
                     _passing()], flakiness=_repaired(applied=True))
    state, _ = _run(workspace, rec)

    assert rec.calls["repair_build"] == 1
    assert rec.calls["test"] == 3
    assert rec.calls["flakiness"] == 1
    assert state.reverified
    assert state.test.all_passed


def test_unapplied_repair_does_not_reverify(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_passing()], flakiness=_repaired(applied=False))
    state, _ = _run(workspace, rec)

    assert state.reverified is False
    assert rec.calls["test"] == 1


def test_stable_build_does_not_reverify(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_passing()], flakiness=_stable())
    state, _ = _run(workspace, rec)

    assert state.reverified is False
    assert rec.calls["test"] == 1


# ---------------------------------------------------------------------------
# Routing units
# ---------------------------------------------------------------------------

def test_route_test_failure_names_the_failing_tests():
    outcome = _failing(("check node version", "/app/index.js exists"), total=10)
    decision = route_test_failure(outcome)

    assert decision.target == MODULE_1
    assert "check node version" in decision.instruction
    assert "/app/index.js exists" in decision.instruction
    assert "2 of 10" in decision.reason


def test_route_test_failure_passes_through_when_green():
    assert route_test_failure(_passing()).target == MODULE_3


def test_patch_instruction_caps_the_list():
    outcome = _failing(tuple(f"test {i}" for i in range(20)), total=40)
    instruction = build_patch_instruction(outcome)
    assert "and 12 more" in instruction


def test_route_flakiness_targets():
    assert route_flakiness(_repaired(applied=True)).target == MODULE_2
    assert route_flakiness(_repaired(applied=False)).target == DONE
    assert route_flakiness(_stable()).target == DONE


def test_should_stop_on_cap():
    state = PipelineState(workspace_path="/w", dockerfile_path="/w/D",
                          feedback_rounds=MAX_FEEDBACK_ROUNDS)
    stop, reason = should_stop(state)
    assert stop and "feedback limit" in reason


def test_should_stop_on_repeated_signature():
    state = PipelineState(workspace_path="/w", dockerfile_path="/w/D")
    state.test_signatures = ["abc", "abc"]
    stop, reason = should_stop(state)
    assert stop and "again" in reason


def test_should_not_stop_on_first_failure():
    state = PipelineState(workspace_path="/w", dockerfile_path="/w/D")
    state.test_signatures = ["abc"]
    assert should_stop(state)[0] is False


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def test_state_serialises_for_the_done_event(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_failing(), _passing()])
    state, _ = _run(workspace, rec)

    payload = state.to_dict()
    assert payload["feedback_rounds"] == 1
    assert payload["stage"] == "done"
    assert isinstance(payload["history"], list)


def test_agent_events_carry_the_agent_stage(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_failing(), _passing()])
    _, events = _run(workspace, rec)

    agent_events = [m for s, _, m in events if s == "agent"]
    assert any("routing back" in m.lower() for m in agent_events)
    assert any("feedback round 1 of 2" in m.lower() for m in agent_events)


# ---------------------------------------------------------------------------
# Configured feedback cap (was displayed but never enforced)
# ---------------------------------------------------------------------------

def test_configured_cap_is_honoured_not_just_displayed(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    # Distinct failures each time so the signature guard does not fire first.
    rec = _Recorder([_failing((f"t{i}",)) for i in range(8)])
    state, _ = _run(workspace, rec, max_feedback_rounds=4)

    assert state.feedback_rounds == 4          # not the module default of 2
    assert rec.calls["flakiness"] == 1


def test_cap_of_one_allows_a_single_round(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_failing(("a",)), _failing(("b",)), _failing(("c",))])
    state, _ = _run(workspace, rec, max_feedback_rounds=1)

    assert state.feedback_rounds == 1
    assert rec.calls["patch"] == 1


def test_cap_of_zero_routes_nothing_back(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_failing()])
    state, _ = _run(workspace, rec, max_feedback_rounds=0)

    assert state.feedback_rounds == 0
    assert rec.calls["patch"] == 0
    assert rec.calls["flakiness"] == 1         # still reports the verdict


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancellation_stops_before_the_next_stage(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_passing()])
    request = PipelineRequest(workspace_path=str(workspace))

    state = run_pipeline(request, None, rec.as_modules(), cancelled=lambda: True)

    assert rec.calls["test"] == 0              # stopped at the first boundary
    assert rec.calls["flakiness"] == 0
    assert state.stage == "done"
    assert any("cancelled" in r.message.lower() for r in state.history)


def test_cancellation_after_tests_skips_flakiness(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_passing()])
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 1                  # allow the first boundary only

    state = run_pipeline(PipelineRequest(workspace_path=str(workspace)),
                         None, rec.as_modules(), cancelled=cancelled)

    assert rec.calls["test"] == 1
    assert rec.calls["flakiness"] == 0


def test_no_cancellation_runs_everything(workspace):
    (workspace / "Dockerfile").write_text("FROM alpine\n")
    rec = _Recorder([_passing()])
    run_pipeline(PipelineRequest(workspace_path=str(workspace)),
                 None, rec.as_modules(), cancelled=lambda: False)
    assert rec.calls["test"] == 1
    assert rec.calls["flakiness"] == 1
