"""A single-module run must be remembered, so /chat can answer from it.

Before this, only the coordinated /pipeline/run stored anything: using the
Generate Dockerfile, Generate Tests or Repair Flakiness buttons on their own
left the assistant insisting that no pipeline had been run.
"""

import pytest
from fastapi.testclient import TestClient

import main
from coordination.state import FlakinessOutcome, GenerationOutcome
from test_generation.data_structures import (
    PipelineResult,
    TestCaseResult,
    TestRunResult,
)


@pytest.fixture(autouse=True)
def clean_memory():
    main._last_state.clear()
    yield
    main._last_state.clear()


# ---------------------------------------------------------------------------
# The store itself
# ---------------------------------------------------------------------------

def test_a_partial_run_is_recalled_for_its_own_workspace(tmp_path):
    main._remember_module_run(
        str(tmp_path), str(tmp_path / "Dockerfile"),
        generation=GenerationOutcome(success=True, attempts=2, message="ok"),
    )
    state = main._recall_run(str(tmp_path))

    assert state is not None
    assert state.stage == "done"
    assert state.test is None and state.flakiness is None
    # The one filled section is what chat is given as context.
    assert any("generation: succeeded" in line for line in state.summary_lines())


def test_another_workspace_does_not_see_the_run(tmp_path):
    main._remember_module_run(
        str(tmp_path), str(tmp_path / "Dockerfile"),
        flakiness=FlakinessOutcome(verdict="stable", message="stable"),
    )
    assert main._recall_run("/some/other/project") is None


def test_a_flakiness_only_run_summarises_without_the_other_modules(tmp_path):
    main._remember_module_run(
        str(tmp_path), str(tmp_path / "Dockerfile"),
        flakiness=FlakinessOutcome(
            verdict="non-deterministic", is_flaky=True,
            failing_instruction="RUN apt-get update", message="2 of 3 builds failed"),
    )
    lines = main._recall_run(str(tmp_path)).summary_lines()

    assert any("non-deterministic" in line for line in lines)
    assert not any("Dockerfile generation" in line for line in lines)


# ---------------------------------------------------------------------------
# Generate Tests, through the real endpoint
# ---------------------------------------------------------------------------

def _result(output_path, passed, total):
    cases = [TestCaseResult(name=f"t{i}", passed=i < passed, errors=[])
             for i in range(total)]
    return PipelineResult(
        output_path=output_path,
        test_run=TestRunResult(total=total, passed=passed, failed=total - passed,
                               results=cases, raw_output="{}"),
    )


def test_generate_tests_button_is_remembered(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM alpine\n")
    spec = str(tmp_path / ".dockagent" / "tests" / "container-structure-test.yaml")

    class FakePipeline:
        def run(self, **kwargs):
            return _result(spec, passed=7, total=10)

    monkeypatch.setattr("test_generation.pipeline.TestPipeline", FakePipeline)

    with TestClient(main.app) as client:
        response = client.post("/pipeline/test", json={
            "dockerfile_path": str(dockerfile),
            "workspace_path": str(tmp_path),
        })
        assert response.status_code == 200
        assert "7 of 10 tests passed" in response.text

    state = main._recall_run(str(tmp_path))
    assert state is not None, "the test run was not remembered"
    assert state.test is not None and state.test.executed
    assert (state.test.passed, state.test.total) == (7, 10)
    assert len(state.test.failing) == 3
    assert any("7/10 passed" in line for line in state.summary_lines())


def test_a_spec_that_could_not_run_is_still_remembered(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM alpine\n")

    class FakePipeline:
        def run(self, **kwargs):
            return PipelineResult(
                output_path=str(tmp_path / "spec.yaml"),
                execution_error="'docker' executable not found on PATH.",
            )

    monkeypatch.setattr("test_generation.pipeline.TestPipeline", FakePipeline)

    with TestClient(main.app) as client:
        client.post("/pipeline/test", json={
            "dockerfile_path": str(dockerfile),
            "workspace_path": str(tmp_path),
        })

    state = main._recall_run(str(tmp_path))
    assert state is not None
    assert not state.test.executed
    assert "docker" in state.test.warning
