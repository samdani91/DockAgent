"""What the agent knows about a run, and what it hands back.

PipelineState is append-only: every stage records what it did, so the closing
summary and any later question can be answered from the state alone.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict


@dataclass
class StageRecord:
    """One thing that happened, in order."""
    stage: str                  # "generate" | "test" | "flakiness" | "agent"
    outcome: str                # "success" | "failure" | "skipped" | "routed"
    message: str
    feedback_round: int = 0


@dataclass
class GenerationOutcome:
    success: bool
    dockerfile_path: str | None = None
    attempts: int = 0
    message: str = ""
    optimized: bool = False
    size_reduction_pct: float | None = None


@dataclass
class FailingTest:
    name: str
    errors: list[str] = field(default_factory=list)


@dataclass
class TestOutcome:
    # Stops pytest trying to collect this as a test class purely for its name.
    __test__ = False

    executed: bool = False
    total: int = 0
    passed: int = 0
    failed: int = 0
    failing: list[FailingTest] = field(default_factory=list)
    spec_path: str | None = None
    message: str = ""
    warning: str | None = None

    @property
    def all_passed(self) -> bool:
        """True only when tests actually ran and none failed.

        A spec that was written but never executed is not a pass.
        """
        return self.executed and self.failed == 0

    def signature(self) -> str:
        """Stable key for 'the same tests failed again'."""
        basis = "|".join(
            sorted(f"{t.name}:{(t.errors or [''])[0][:80]}" for t in self.failing)
        )
        return hashlib.sha256(basis.encode()).hexdigest()[:16] if basis else ""


@dataclass
class FlakinessOutcome:
    verdict: str = ""           # non-deterministic | deterministic-failure | stable
    is_flaky: bool = False
    needs_repair: bool = False
    repaired: bool = False
    applied: bool = False
    repaired_path: str | None = None
    attempts: int = 0
    failing_instruction: str = ""
    message: str = ""


@dataclass
class PipelineState:
    workspace_path: str
    dockerfile_path: str
    image_name: str = ""
    stage: str = "generate"     # generate | test | flakiness | done
    generation: GenerationOutcome | None = None
    test: TestOutcome | None = None
    flakiness: FlakinessOutcome | None = None
    feedback_rounds: int = 0
    reverified: bool = False    # Feedback B runs at most once
    history: list[StageRecord] = field(default_factory=list)
    test_signatures: list[str] = field(default_factory=list)

    def record(self, stage: str, outcome: str, message: str) -> None:
        self.history.append(StageRecord(
            stage=stage, outcome=outcome, message=message,
            feedback_round=self.feedback_rounds,
        ))

    def to_dict(self) -> dict:
        """Serialised for the SSE done event."""
        return asdict(self)

    def summary_lines(self) -> list[str]:
        """Compact human-readable recap, used for prompts and the UI."""
        lines: list[str] = []
        if self.generation:
            lines.append(
                f"Dockerfile generation: "
                f"{'succeeded' if self.generation.success else 'failed'}"
                f" after {self.generation.attempts} attempt(s)."
            )
        if self.test:
            if self.test.executed:
                lines.append(
                    f"Container tests: {self.test.passed}/{self.test.total} passed."
                )
                for t in self.test.failing[:10]:
                    detail = f" — {t.errors[0]}" if t.errors else ""
                    lines.append(f"  failed: {t.name}{detail}")
            else:
                lines.append("Container tests: written but not executed.")
        if self.flakiness:
            lines.append(f"Flakiness: {self.flakiness.verdict}.")
            if self.flakiness.failing_instruction:
                lines.append(f"  failing instruction: {self.flakiness.failing_instruction}")
            if self.flakiness.repaired:
                lines.append(f"  a repair was produced ({self.flakiness.attempts} attempt(s)).")
        if self.feedback_rounds:
            lines.append(f"Feedback rounds used: {self.feedback_rounds}.")
        return lines
