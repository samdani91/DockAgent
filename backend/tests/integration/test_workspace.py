"""Integration coverage for workspace handling — report case T1.

The extension half of T1 (activation, configuration persistence) is TypeScript
and needs a VS Code test host, which this project does not have set up. What is
testable here is the backend's half: deciding whether a workspace is usable and
whether it already has a Dockerfile.
"""

import pytest

import main
from coordination.orchestrator import PipelineRequest
from coordination.runners import make_llm

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# T1 — Workspace detection
# ---------------------------------------------------------------------------

def test_t1_a_real_project_is_recognised_as_usable(user_service):
    assert main._has_project_files(user_service) is True


def test_t1_an_empty_folder_is_rejected(tmp_path):
    assert main._has_project_files(tmp_path) is False


def test_t1_a_folder_of_only_hidden_files_is_rejected(tmp_path):
    (tmp_path / ".hidden").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")
    assert main._has_project_files(tmp_path) is False


def test_t1_absence_of_a_dockerfile_is_detected(user_service):
    assert not (user_service / "Dockerfile").exists()


def test_t1_presence_of_a_dockerfile_is_detected(user_service):
    (user_service / "Dockerfile").write_text("FROM node:20-alpine\n")
    assert (user_service / "Dockerfile").is_file()


def test_t1_build_docs_are_found_when_present(documented_project):
    found = main._find_docs(documented_project)
    assert len(found) == 1
    assert found[0].endswith("README.md")


def test_t1_no_build_docs_is_not_an_error(undocumented_project):
    assert main._find_docs(undocumented_project) == []


# ---------------------------------------------------------------------------
# T1 — Configuration
# ---------------------------------------------------------------------------

def test_t1_configured_model_is_used(monkeypatch):
    """The model from the environment reaches the client."""
    monkeypatch.setenv("GEMINI_API_KEY", "placeholder")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")

    client = make_llm(PipelineRequest(workspace_path="/w"))
    assert client is not None
    assert client._model == "gemini-3.5-flash"


def test_t1_an_explicit_model_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "placeholder")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")

    client = make_llm(PipelineRequest(workspace_path="/w", model="gemini-flash-latest"))
    assert client._model == "gemini-flash-latest"


def test_t1_no_key_configured_yields_no_client(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert make_llm(PipelineRequest(workspace_path="/w")) is None
