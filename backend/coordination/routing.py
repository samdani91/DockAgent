"""Failure routing — deliberately deterministic.

The agent decides where a failure goes using rules, not a model. Only the
explanation of that decision is LLM-generated (see explain.py). Keeping the
boundary here means a run is reproducible and a routing bug is debuggable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import FlakinessOutcome, PipelineState, TestOutcome

log = logging.getLogger("dockagent.agent")

#: How many times a container-test failure may be routed back to Module 1.
MAX_FEEDBACK_ROUNDS: int = 2

# Routing targets
MODULE_1 = "module_1"      # Dockerfile generation / repair
MODULE_2 = "module_2"      # container test generation + execution
MODULE_3 = "module_3"      # flakiness detection + repair
DONE = "done"

#: Cap on how many failing tests are named in a patch instruction. Beyond this
#: the prompt stops being actionable and starts being a wall of text.
_MAX_LISTED_FAILURES = 8


@dataclass
class RoutingDecision:
    target: str
    reason: str
    instruction: str = ""   # patch instruction, when routing to Module 1


def route_test_failure(test_outcome: "TestOutcome") -> RoutingDecision:
    """Where does a container-test result go next?"""
    if test_outcome.all_passed:
        return RoutingDecision(
            target=MODULE_3,
            reason="All container tests passed; continuing to flakiness detection.",
        )

    if not test_outcome.executed:
        # The spec exists but never ran — nothing was learned about the image,
        # so there is no failure to route back.
        return RoutingDecision(
            target=MODULE_3,
            reason="Tests were generated but not executed; nothing to route back.",
        )

    return RoutingDecision(
        target=MODULE_1,
        reason=(
            f"{test_outcome.failed} of {test_outcome.total} container tests failed; "
            f"routing back to Dockerfile repair."
        ),
        instruction=build_patch_instruction(test_outcome),
    )


def route_flakiness(flakiness_outcome: "FlakinessOutcome") -> RoutingDecision:
    """After Module 3, does anything need re-verifying?"""
    if flakiness_outcome.repaired and flakiness_outcome.applied:
        return RoutingDecision(
            target=MODULE_2,
            reason=(
                "The Dockerfile was repaired and applied; re-running the container "
                "tests to confirm they still pass."
            ),
        )
    if flakiness_outcome.repaired:
        return RoutingDecision(
            target=DONE,
            reason=(
                "A repair was produced but not applied, so the tested image is "
                "unchanged. Review the diff before re-running the tests."
            ),
        )
    if flakiness_outcome.needs_repair:
        return RoutingDecision(
            target=DONE,
            reason="The build failure could not be repaired automatically.",
        )
    return RoutingDecision(
        target=DONE,
        reason="No instability observed; nothing to re-verify.",
    )


def build_patch_instruction(test_outcome: "TestOutcome") -> str:
    """Turn failing tests into an instruction the repair prompt can act on.

    Delivered through the existing `scan_hint` channel, which is prepended to
    error-driven repair prompts.
    """
    lines = [
        "The image builds, but it failed these container structure checks:",
    ]
    for test in test_outcome.failing[:_MAX_LISTED_FAILURES]:
        detail = f" — {test.errors[0]}" if test.errors else ""
        lines.append(f"  - {test.name}{detail}")

    remaining = len(test_outcome.failing) - _MAX_LISTED_FAILURES
    if remaining > 0:
        lines.append(f"  - … and {remaining} more")

    lines.append("")
    lines.append(
        "Change the Dockerfile so these checks hold. Fix only what these failures "
        "require — leave the base image, entry point and exposed port alone unless "
        "a failure names them."
    )
    return "\n".join(lines) + "\n\n"


def should_stop(
    state: "PipelineState", cap: int = MAX_FEEDBACK_ROUNDS
) -> tuple[bool, str]:
    """Guard against looping on something the agent cannot fix.

    *cap* comes from the request; without it the caller's configured limit was
    reported to the user but never actually applied.

    Returns (stop, reason).
    """
    if state.feedback_rounds >= cap:
        return True, (
            f"Reached the feedback limit of {cap} round(s) without "
            f"getting the container tests to pass."
        )

    # The same tests failing the same way twice means the repair is not landing.
    signatures = [s for s in state.test_signatures if s]
    if len(signatures) >= 2 and signatures[-1] == signatures[-2]:
        return True, (
            "The same container tests failed again after a repair, so further "
            "rounds are unlikely to help."
        )

    return False, ""
