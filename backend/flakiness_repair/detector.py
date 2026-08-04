"""Detect Dockerfile instability by building repeatedly with the cache disabled.

FLAKIDOCK's builder stage builds a candidate `n` times and treats it as
non-flaky when no failure appears (paper: n = 2 covers >90% of their cases).

An honest limitation of running this inside an editor: the paper established
flakiness by rebuilding 8,132 projects weekly for nine months. In one session
we can only observe *current* behaviour, so a clean run is reported as "no
instability observed", never as "not flaky".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .preprocess import ErrorFeatures, preprocess

log = logging.getLogger("dockagent.detect")

if TYPE_CHECKING:
    from dockerfile_generation.build import DockerBuilder

DEFAULT_ITERATIONS: int = 2   # paper's n

# Verdicts
NON_DETERMINISTIC = "non-deterministic"
DETERMINISTIC_FAILURE = "deterministic-failure"
STABLE = "stable"


@dataclass
class BuildObservation:
    index: int
    success: bool
    error: ErrorFeatures | None = None
    log: str = ""


@dataclass
class FlakinessReport:
    verdict: str
    iterations: int
    successes: int
    failures: int
    observations: list[BuildObservation] = field(default_factory=list)

    @property
    def is_flaky(self) -> bool:
        """True only for observed instability — mixed outcomes across builds."""
        return self.verdict == NON_DETERMINISTIC

    @property
    def needs_repair(self) -> bool:
        """Both mixed outcomes and consistent failure are worth repairing."""
        return self.verdict in (NON_DETERMINISTIC, DETERMINISTIC_FAILURE)

    @property
    def distinct_errors(self) -> list[str]:
        seen: list[str] = []
        for obs in self.observations:
            if obs.error and obs.error.signature() not in seen:
                seen.append(obs.error.signature())
        return seen

    @property
    def primary_error(self) -> ErrorFeatures | None:
        """The first failure — what a repair attempt should target."""
        for obs in self.observations:
            if not obs.success and obs.error:
                return obs.error
        return None

    def summary(self) -> str:
        if self.verdict == NON_DETERMINISTIC:
            return (
                f"Flaky: {self.successes} of {self.iterations} builds succeeded "
                f"and {self.failures} failed with identical input."
            )
        if self.verdict == DETERMINISTIC_FAILURE:
            return (
                f"Failed all {self.iterations} builds. This is a consistent "
                f"failure — it may be flakiness that has become permanent, or a "
                f"Dockerfile that is simply broken."
            )
        return (
            f"No instability observed across {self.iterations} builds. This is "
            f"not proof the Dockerfile is stable — flakiness often only appears "
            f"as external dependencies change over time."
        )


def detect(
    dockerfile: str,
    context_dir: str,
    builder: "DockerBuilder",
    iterations: int = DEFAULT_ITERATIONS,
    progress: Callable[[str, str], None] | None = None,
) -> FlakinessReport:
    """Build *dockerfile* *iterations* times and classify what happened.

    The builder must have caching disabled — a cached layer is exactly what
    hides flaky behaviour.
    """
    emit = progress or (lambda _step, _msg: None)
    if iterations < 1:
        raise ValueError("iterations must be at least 1")

    observations: list[BuildObservation] = []
    for i in range(1, iterations + 1):
        emit("detect", f"Build {i} of {iterations} (no cache)…")
        result = builder.build(dockerfile, context_dir)

        if result.success:
            observations.append(BuildObservation(index=i, success=True, log=result.log))
            emit("detect", f"Build {i} of {iterations} succeeded.")
        else:
            features = preprocess(result.log, dockerfile)
            observations.append(
                BuildObservation(index=i, success=False, error=features, log=result.log)
            )
            emit("detect", f"Build {i} of {iterations} failed.")

    successes = sum(1 for o in observations if o.success)
    failures = len(observations) - successes

    if successes and failures:
        verdict = NON_DETERMINISTIC
    elif failures:
        verdict = DETERMINISTIC_FAILURE
    else:
        verdict = STABLE

    log.info("verdict %s — %d/%d builds succeeded", verdict, successes, iterations)
    for observation in observations:
        if observation.error and observation.error.dockerfile_error_line:
            log.info("  failing instruction: %s",
                     observation.error.dockerfile_error_line[:100])
            break

    return FlakinessReport(
        verdict=verdict,
        iterations=iterations,
        successes=successes,
        failures=failures,
        observations=observations,
    )
