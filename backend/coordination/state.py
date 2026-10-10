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
    #: What the suite covers. Without this the agent could only repeat the
    #: pass count — asked "explain the test result" on a 1/1 run it had
    #: nothing to describe and fell back to guessing what the test might be.
    image_name: str = ""
    command_tests: int = 0
    file_tests: int = 0
    metadata_tests: int = 0
    #: A sample of what passed, so the agent can name tests rather than
    #: describe them in the abstract. Capped to keep the prompt small.
    passing: list[str] = field(default_factory=list)

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
    #: The evidence behind the verdict, rather than the verdict alone.
    iterations: int = 0
    successes: int = 0
    failures: int = 0
    #: Fault types retrieved from the knowledge base for the repair prompt.
    retrieved: list[str] = field(default_factory=list)
    #: The error itself, already distilled by the paper's pre-processing step.
    #: Without it the agent could name the failing instruction but not say why
    #: it failed, and answered "the log does not include the terminal output"
    #: before guessing. The raw log stays out: it is unbounded, it echoes build
    #: args and tokens, and it is mostly progress chatter.
    stderr: str = ""
    error_segment: str = ""


#: Longest failure text carried into the recap. The recap is a prompt, not UI,
#: so a whole build log is bounded here rather than spent on tokens.
_MAX_REASON_CHARS = 600


def _dedupe(text: str) -> str:
    """Drop lines already contained in another, longer line.

    BuildKit reports the same failure twice — once bare and once behind
    "failed to build: failed to solve:" — which doubled the line for no
    added meaning.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    kept = [
        line for i, line in enumerate(lines)
        if not any(line in other for j, other in enumerate(lines)
                   if j != i and len(other) > len(line))
    ]
    return "\n".join(kept)


def _condense(text: str, limit: int = _MAX_REASON_CHARS) -> str:
    """One line, bounded, keeping the tail where build errors actually are."""
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    head = limit // 4
    tail = limit - head
    return f"{clean[:head]} […] {clean[-tail:]}"


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
        if self.dockerfile_path:
            lines.append(f"Dockerfile: {self.dockerfile_path}")

        if self.generation:
            g = self.generation
            lines.append(
                f"Dockerfile generation: "
                f"{'succeeded' if g.success else 'failed'}"
                f" after {g.attempts} attempt(s)."
            )
            # Without the message a failed run said only "failed after 6
            # attempt(s)", which is too thin to answer "why?" — and thin
            # context is what the model fills in with guesses.
            if not g.success and g.message:
                lines.append(f"  reason: {_condense(g.message)}")
            if g.optimized:
                reduction = (
                    f" ({g.size_reduction_pct:.1f}% smaller)"
                    if g.size_reduction_pct is not None else ""
                )
                lines.append(f"  multi-stage optimization applied{reduction}.")

        if self.test:
            t_out = self.test
            if t_out.executed:
                lines.append(
                    f"Container tests: {t_out.passed}/{t_out.total} passed."
                )
                lines.extend(f"  {line}" for line in self._coverage_lines())
                for t in t_out.failing[:10]:
                    detail = f" — {t.errors[0]}" if t.errors else ""
                    lines.append(f"  failed: {t.name}{detail}")
                if t_out.passing:
                    shown = ", ".join(t_out.passing[:12])
                    more = (
                        f", and {len(t_out.passing) - 12} more"
                        if len(t_out.passing) > 12 else ""
                    )
                    lines.append(f"  passed: {shown}{more}")
            else:
                lines.append(
                    t_out.message
                    if t_out.message.startswith("Container tests could not run:")
                    else "Container tests: written but not executed."
                )
                lines.extend(f"  {line}" for line in self._coverage_lines())
            if t_out.spec_path:
                lines.append(f"  specification: {t_out.spec_path}")

        if self.flakiness:
            f_out = self.flakiness
            lines.append(f"Flakiness: {f_out.verdict}.")
            if f_out.iterations:
                lines.append(
                    f"  {f_out.successes} of {f_out.iterations} no-cache builds "
                    f"succeeded, {f_out.failures} failed."
                )
            if f_out.failing_instruction:
                lines.append(f"  failing instruction: {f_out.failing_instruction}")
            if f_out.stderr:
                lines.append(f"  reported error: {_condense(_dedupe(f_out.stderr), 400)}")
            if f_out.error_segment:
                # The tail: a failing step ends with the reason it failed.
                tail = f_out.error_segment.strip().splitlines()[-12:]
                lines.append("  output of the failing step (last lines):")
                lines.extend(f"    {line}" for line in tail)
            if f_out.retrieved:
                lines.append(
                    f"  similar repairs retrieved: {', '.join(f_out.retrieved)}"
                )
            if f_out.repaired:
                where = (
                    "applied to the Dockerfile" if f_out.applied
                    else f"written to {f_out.repaired_path}"
                )
                lines.append(
                    f"  a repair was produced in {f_out.attempts} attempt(s) "
                    f"and {where}."
                )
            elif f_out.needs_repair and f_out.attempts:
                lines.append(
                    f"  no repair could be validated after "
                    f"{f_out.attempts} attempt(s)."
                )

        if self.feedback_rounds:
            lines.append(f"Feedback rounds used: {self.feedback_rounds}.")
        if self.image_name:
            lines.append(f"Image under test: {self.image_name}")
        return lines

    def _coverage_lines(self) -> list[str]:
        """What the generated suite checks, by kind."""
        t_out = self.test
        if t_out is None:
            return []
        parts = [
            (t_out.command_tests, "command test"),
            (t_out.file_tests, "file existence test"),
            (t_out.metadata_tests, "metadata check"),
        ]
        present = [
            f"{n} {label}{'' if n == 1 else 's'}" for n, label in parts if n
        ]
        return [f"covering {', '.join(present)}."] if present else []
