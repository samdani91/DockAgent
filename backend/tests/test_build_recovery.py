"""S0 build errors are distinct from CST failures and use Module 1 repair."""

import subprocess

import pytest

from coordination import runners
from coordination.orchestrator import PipelineRequest
from coordination.state import PipelineState
from dockerfile_generation.build import BuildResult
from dockerfile_generation.context import ProjectContext
from dockerfile_generation.loop import LoopResult
from test_generation.builder import ImageBuildError, build_image


def test_s0_build_failure_preserves_log_and_cleans_temp_file(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM alpine\nRUN false\n")
    log = "ERROR: failed to solve: process returned 1"
    monkeypatch.setattr(
        "test_generation.builder.run_process",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=log.encode()),
    )

    with pytest.raises(ImageBuildError) as error:
        build_image(str(dockerfile), str(tmp_path), "test-image")

    assert error.value.exit_code == 1
    assert error.value.log == log
    assert not list(tmp_path.glob(".dockagent_build_*.dockerfile"))


@pytest.mark.parametrize("success", [True, False])
def test_repair_test_build_writes_only_validated_candidate(
    tmp_path, monkeypatch, success
):
    dockerfile = tmp_path / "Dockerfile"
    original = "FROM alpine\nRUN false\n"
    candidate = "FROM alpine\nRUN echo one && echo two\n"
    dockerfile.write_text(original)
    state = PipelineState(workspace_path=str(tmp_path), dockerfile_path=str(dockerfile))
    request = PipelineRequest(workspace_path=str(tmp_path), max_attempts=3)
    failure = ImageBuildError(1, "ERROR: failed to solve: false returned 1")
    built = []

    class FakeRealBuilder:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == request.build_timeout

        def build(self, content, context_dir):
            built.append(content)
            assert context_dir == str(tmp_path)
            return BuildResult(success=success, exit_code=0 if success else 1,
                               log="build result")

    def fake_loop(**kwargs):
        assert kwargs["initial_dockerfile"] == original
        assert kwargs["initial_result"].log == failure.log
        assert kwargs["max_attempts"] == 3
        result = kwargs["builder"].build(candidate, kwargs["context_dir"])
        return LoopResult(success=result.success, dockerfile=candidate,
                          attempts=2, last_error=None if success else "still broken",
                          last_log=result.log)

    monkeypatch.setattr(runners, "_require_llm", lambda _request: object())
    monkeypatch.setattr("dockerfile_generation.build.RealDockerBuilder", FakeRealBuilder)
    monkeypatch.setattr("dockerfile_generation.context.build_context",
                        lambda *_args: ProjectContext(text="project"))
    monkeypatch.setattr("dockerfile_generation.loop.run_loop", fake_loop)

    outcome = runners.repair_test_build(request, state, failure,
                                        lambda *_args: None)

    assert built == ["FROM alpine\nRUN echo one\nRUN echo two"]
    assert outcome.success is success
    assert dockerfile.read_text() == (candidate if success else original)
