"""Shared setup for the integration suite.

These tests use real Docker, a real model and real projects. They are slow and
cost API calls, so a plain `pytest` run skips them:

    pytest                      # 195 unit tests, no Docker, no key
    pytest -m integration       # this suite
    pytest -m "integration and not needs_llm"

Anything missing (Docker down, no key, no network) skips rather than fails —
a missing prerequisite is not a defect in DockAgent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
load_dotenv(BACKEND / ".env")

PROJECTS = Path(__file__).parent / "projects"

#: The user's own Node service, used where a real third-party project matters.
USER_SERVICE = Path.home() / "Desktop" / "User_Service"


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

def _docker_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=20
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _llm_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip()
                or os.environ.get("OPENAI_API_KEY", "").strip())


DOCKER = _docker_available()
LLM = _llm_available()


def pytest_collection_modifyitems(config, items):
    """Skip what cannot run here, with a reason that says why."""
    no_docker = pytest.mark.skip(reason="Docker is not available")
    no_llm = pytest.mark.skip(reason="no provider API key configured")

    for item in items:
        if "integration" not in item.keywords:
            continue
        if not DOCKER:
            item.add_marker(no_docker)
        if "needs_llm" in item.keywords and not LLM:
            item.add_marker(no_llm)


# ---------------------------------------------------------------------------
# Projects under test
# ---------------------------------------------------------------------------

def _copy_project(name: str, tmp_path: Path) -> Path:
    """A writable copy, so a test that writes a Dockerfile cannot dirty the repo."""
    source = PROJECTS / name
    if not source.is_dir():
        pytest.skip(f"fixture project missing: {source}")
    target = tmp_path / name
    shutil.copytree(source, target)
    return target


@pytest.fixture
def documented_project(tmp_path) -> Path:
    """A FastAPI service whose README states how to build and run it."""
    return _copy_project("fastapi_documented", tmp_path)


@pytest.fixture
def undocumented_project(tmp_path) -> Path:
    """A working Express service with no README at all."""
    return _copy_project("express_undocumented", tmp_path)


@pytest.fixture
def user_service(tmp_path) -> Path:
    """The user's own Node service — a real project, not written for tests."""
    if not USER_SERVICE.is_dir():
        pytest.skip(f"User_Service not found at {USER_SERVICE}")
    target = tmp_path / "User_Service"
    shutil.copytree(
        USER_SERVICE, target,
        ignore=shutil.ignore_patterns("node_modules", ".git", ".dockagent"),
    )
    return target


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def builder():
    from dockerfile_generation.build import RealDockerBuilder
    return RealDockerBuilder(timeout=600)


@pytest.fixture
def nocache_builder():
    """Caching is what hides flaky behaviour, so flakiness work needs this."""
    from dockerfile_generation.build import RealDockerBuilder
    return RealDockerBuilder(timeout=600, no_cache=True)


@pytest.fixture
def llm():
    from dockerfile_generation.llm import GeminiClient
    return GeminiClient()


@pytest.fixture
def built_image(builder):
    """Build a Dockerfile and remove the image afterwards.

    Returns a callable so a test can build more than one.
    """
    created: list[str] = []

    def _build(dockerfile: str, context_dir: Path):
        result = builder.build(dockerfile, str(context_dir))
        if result.success and result.image_id:
            created.append(result.image_id)
        return result

    yield _build

    for image_id in created:
        subprocess.run(["docker", "rmi", "-f", image_id],
                       capture_output=True, timeout=60)


@pytest.fixture
def tagged_image():
    """Build and tag an image, removing the tag afterwards."""
    tags: list[str] = []

    def _build(dockerfile_path: Path, context_dir: Path, tag: str):
        subprocess.run(
            ["docker", "build", "-f", str(dockerfile_path), "-t", tag, "."],
            cwd=str(context_dir), capture_output=True, timeout=600, check=True,
        )
        tags.append(tag)
        return tag

    yield _build

    for tag in tags:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=60)
