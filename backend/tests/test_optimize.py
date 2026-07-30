"""Tests for Phase B optimization — every outcome branch, no Docker required.

The safety property under test: optimization must never leave the caller with a
Dockerfile worse than the one it was given.
"""

import pytest

from dockerfile_generation.build import BuildResult, FakeDockerBuilder
from dockerfile_generation.context import ProjectContext
from dockerfile_generation.optimize import MULTI_STAGE, optimize

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ORIGINAL = (
    "FROM node:18\n"
    "WORKDIR /app\n"
    "COPY package.json .\n"
    "RUN npm install\n"
    "COPY . .\n"
    "EXPOSE 8081\n"
    'CMD ["node", "src/index.js"]'
)

_MULTI_STAGE = (
    "# ── Stage 1: dependencies ──\n"
    "FROM node:18 AS deps\n"
    "WORKDIR /app\n"
    "COPY package.json .\n"
    "RUN npm install --omit=dev\n"
    "\n"
    "# ── Stage 2: runtime ──\n"
    "FROM node:18-slim\n"
    "WORKDIR /app\n"
    "COPY --from=deps /app/node_modules ./node_modules\n"
    "COPY . .\n"
    "EXPOSE 8081\n"
    'CMD ["node", "src/index.js"]'
)

_CONTEXT = ProjectContext(
    text="Express service",
    scan_hint="Critical project facts:\n  Port: 8081\n\n",
)


class _FakeLLM:
    def __init__(self, reply: str = _MULTI_STAGE) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.reply


def _sizer(mapping: dict[str, int]):
    return lambda image_id: mapping.get(image_id)


def _ok(image_id: str) -> BuildResult:
    return BuildResult(success=True, exit_code=0, log="ok", image_id=image_id)


def _fail(log: str = "ERROR: something broke") -> BuildResult:
    return BuildResult(success=False, exit_code=1, log=log)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_optimized_build_that_is_smaller_is_kept():
    builder = FakeDockerBuilder([_ok("orig"), _ok("opt")])
    result = optimize(
        _ORIGINAL, _CONTEXT, "/repo", builder, _FakeLLM(),
        sizer=_sizer({"orig": 1000, "opt": 400}),
    )

    assert result.success is True
    assert result.dockerfile == _MULTI_STAGE
    assert result.original_size == 1000
    assert result.optimized_size == 400
    assert result.repair_attempts == 0
    assert result.reduction_pct == pytest.approx(60.0)


def test_reduction_pct_is_none_when_sizes_unknown():
    builder = FakeDockerBuilder([_ok("orig"), _ok("opt")])
    result = optimize(
        _ORIGINAL, _CONTEXT, "/repo", builder, _FakeLLM(),
        sizer=_sizer({}),  # sizer knows nothing
    )
    assert result.reduction_pct is None


# ---------------------------------------------------------------------------
# Fallback paths — the safety property
# ---------------------------------------------------------------------------

def test_falls_back_when_optimized_is_not_smaller():
    builder = FakeDockerBuilder([_ok("orig"), _ok("opt")])
    result = optimize(
        _ORIGINAL, _CONTEXT, "/repo", builder, _FakeLLM(),
        sizer=_sizer({"orig": 500, "opt": 900}),  # bigger!
    )

    assert result.success is False
    assert result.dockerfile == _ORIGINAL
    assert "not smaller" in result.note


def test_falls_back_when_optimized_equals_original_size():
    builder = FakeDockerBuilder([_ok("orig"), _ok("opt")])
    result = optimize(
        _ORIGINAL, _CONTEXT, "/repo", builder, _FakeLLM(),
        sizer=_sizer({"orig": 500, "opt": 500}),
    )
    assert result.success is False
    assert result.dockerfile == _ORIGINAL


def test_falls_back_when_baseline_no_longer_builds():
    builder = FakeDockerBuilder([_fail()])
    llm = _FakeLLM()
    result = optimize(_ORIGINAL, _CONTEXT, "/repo", builder, llm, sizer=_sizer({}))

    assert result.success is False
    assert result.dockerfile == _ORIGINAL
    assert "no longer builds" in result.note
    assert llm.calls == []  # never wasted an LLM call


def test_falls_back_when_optimized_cannot_be_repaired():
    # baseline OK, then every subsequent build fails
    builder = FakeDockerBuilder([_ok("orig"), _fail()])
    result = optimize(
        _ORIGINAL, _CONTEXT, "/repo", builder, _FakeLLM(),
        sizer=_sizer({"orig": 1000}),
        max_repair_attempts=3,
    )

    assert result.success is False
    assert result.dockerfile == _ORIGINAL
    assert "could not be repaired" in result.note
    assert result.repair_attempts > 0


# ---------------------------------------------------------------------------
# Repair loop integration
# ---------------------------------------------------------------------------

def test_broken_optimized_file_is_repaired_then_kept():
    builder = FakeDockerBuilder([
        _ok("orig"),      # 1. baseline
        _fail(),          # 2. optimized build fails
        _ok("fixed"),     # 3. repair loop's first build succeeds
        _ok("fixed"),     # 4. re-verification
    ])
    result = optimize(
        _ORIGINAL, _CONTEXT, "/repo", builder, _FakeLLM(),
        sizer=_sizer({"orig": 1000, "fixed": 300}),
    )

    assert result.success is True
    assert result.optimized_size == 300
    assert result.repair_attempts == 1


# ---------------------------------------------------------------------------
# Prompt plumbing
# ---------------------------------------------------------------------------

def test_scan_hint_is_included_in_the_rewrite_prompt():
    builder = FakeDockerBuilder([_ok("orig"), _ok("opt")])
    llm = _FakeLLM()
    optimize(_ORIGINAL, _CONTEXT, "/repo", builder, llm,
             sizer=_sizer({"orig": 100, "opt": 50}))

    _, user = llm.calls[0]
    assert "Port: 8081" in user
    assert _ORIGINAL in user
    assert "MULTI-STAGE" in user


def test_markdown_fences_are_stripped_from_the_rewrite():
    fenced = f"```dockerfile\n{_MULTI_STAGE}\n```"
    builder = FakeDockerBuilder([_ok("orig"), _ok("opt")])
    result = optimize(
        _ORIGINAL, _CONTEXT, "/repo", builder, _FakeLLM(reply=fenced),
        sizer=_sizer({"orig": 100, "opt": 50}),
    )
    assert result.dockerfile == _MULTI_STAGE
    assert "```" not in result.dockerfile


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_unsupported_method_raises():
    builder = FakeDockerBuilder([_ok("orig")])
    with pytest.raises(ValueError, match="Unsupported optimization method"):
        optimize(_ORIGINAL, _CONTEXT, "/repo", builder, _FakeLLM(), method="alpine")


def test_progress_callback_receives_messages():
    seen: list[str] = []
    builder = FakeDockerBuilder([_ok("orig"), _ok("opt")])
    optimize(
        _ORIGINAL, _CONTEXT, "/repo", builder, _FakeLLM(),
        sizer=_sizer({"orig": 100, "opt": 50}),
        on_progress=seen.append,
    )
    assert any("baseline" in m.lower() for m in seen)
    assert any(MULTI_STAGE in m for m in seen)
