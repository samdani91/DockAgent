"""Pipeline driver — runs Modules 1-3 and closes the feedback loop.

Matches the SRS Level 1 activity diagram:

    scan ─► [no Dockerfile] ─► Module 1
              │
              ▼
            Module 2 ──tests fail──► Feedback A ──► Module 1 ─┐
              │                                               │
              │◄──────────────────────────────────────────────┘
              ▼
            Module 3 ──repair applied──► Feedback B ──► Module 2 (once)
              │
              ▼
            done

Module entry points are injected so the whole flow can be tested without Docker
or an LLM. `Modules.default()` wires the real ones.

Synchronous by design: every module entry point blocks on subprocesses, so this
runs on a worker thread exactly like the existing endpoints do.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .explain import summarise
from .routing import (
    DONE,
    MAX_FEEDBACK_ROUNDS,
    MODULE_1,
    MODULE_2,
    RoutingDecision,
    route_flakiness,
    route_test_failure,
    should_stop,
)
from .state import (
    FailingTest,
    FlakinessOutcome,
    GenerationOutcome,
    PipelineState,
    TestOutcome,
)

log = logging.getLogger("dockagent.agent")

#: progress(stage, step, message)
Progress = Callable[[str, str, str], None]


@dataclass
class PipelineRequest:
    workspace_path: str
    threshold: float = 0.0
    iterations: int = 2                       # flakiness builds (paper's n)
    max_attempts: int = 6                     # Module 1 build/repair cap
    max_feedback_rounds: int = MAX_FEEDBACK_ROUNDS
    provider: str = "gemini"
    model: str = ""
    optimize: bool = False
    apply: bool = False                       # apply a flakiness repair in place
    build_timeout: int = 900
    execute_timeout: int = 300


@dataclass
class Modules:
    """The four operations the agent can invoke."""
    generate: Callable
    test: Callable
    flakiness: Callable
    patch: Callable                           # Feedback A repair
    llm: object | None = None

    @classmethod
    def default(cls, request: PipelineRequest) -> "Modules":
        from . import runners

        return cls(
            generate=runners.run_generation,
            test=runners.run_tests,
            flakiness=runners.run_flakiness,
            patch=runners.apply_test_feedback,
            llm=runners.make_llm(request),
        )


def run_pipeline(
    request: PipelineRequest,
    progress: Progress | None = None,
    modules: Modules | None = None,
) -> PipelineState:
    """Drive the full pipeline and return the final state."""
    emit: Progress = progress or (lambda _s, _st, _m: None)
    mods = modules if modules is not None else Modules.default(request)

    workspace = Path(request.workspace_path)
    dockerfile_path = workspace / "Dockerfile"
    state = PipelineState(
        workspace_path=str(workspace),
        dockerfile_path=str(dockerfile_path),
    )

    def agent(message: str, step: str = "route") -> None:
        log.info("%s", message)
        emit("agent", step, message)

    # ── 1. Dockerfile ────────────────────────────────────────────────────
    if dockerfile_path.is_file():
        agent("Found an existing Dockerfile — skipping generation.", step="plan")
        state.record("generate", "skipped", "Existing Dockerfile reused.")
    else:
        agent("No Dockerfile in this workspace — generating one first.", step="plan")
        state.stage = "generate"
        state.generation = mods.generate(request, state, emit)
        state.record(
            "generate",
            "success" if state.generation.success else "failure",
            state.generation.message,
        )
        if not state.generation.success:
            agent("Generation failed, so there is nothing to test or check.")
            return _finish(state, mods, agent)

    # ── 2/3. Tests, with Feedback A ──────────────────────────────────────
    while True:
        state.stage = "test"
        state.test = mods.test(request, state, emit)
        state.record(
            "test",
            "success" if state.test.all_passed else "failure",
            state.test.message,
        )
        state.test_signatures.append(state.test.signature())

        decision = route_test_failure(state.test)
        if decision.target != MODULE_1:
            agent(decision.reason)
            break

        stop, reason = should_stop(state)
        if stop:
            # Deliberately not fatal: the developer still wants the flakiness
            # verdict even when the tests cannot be made to pass.
            agent(f"{reason} Continuing to flakiness detection anyway.")
            state.record("agent", "routed", reason)
            break

        round_no = state.feedback_rounds + 1
        agent(
            f"{decision.reason} Feedback round {round_no} of "
            f"{request.max_feedback_rounds}."
        )
        state.record("agent", "routed", decision.reason)
        state.feedback_rounds = round_no

        state.stage = "generate"
        patched = mods.patch(request, state, decision.instruction, emit)
        state.generation = patched
        state.record(
            "generate",
            "success" if patched.success else "failure",
            patched.message,
        )
        if not patched.success:
            agent(
                "The repair could not be applied, so the tests would fail the same "
                "way. Continuing to flakiness detection."
            )
            break

    # ── 4. Flakiness ─────────────────────────────────────────────────────
    state.stage = "flakiness"
    state.flakiness = mods.flakiness(request, state, emit)
    state.record(
        "flakiness",
        "success" if not state.flakiness.needs_repair or state.flakiness.repaired
        else "failure",
        state.flakiness.message,
    )

    # ── 5. Feedback B ────────────────────────────────────────────────────
    decision = route_flakiness(state.flakiness)
    agent(decision.reason)
    if decision.target == MODULE_2 and not state.reverified:
        state.record("agent", "routed", decision.reason)
        state.reverified = True
        state.stage = "test"
        state.test = mods.test(request, state, emit)
        state.record(
            "test",
            "success" if state.test.all_passed else "failure",
            f"Re-verification: {state.test.message}",
        )

    return _finish(state, mods, agent)


def _finish(state: PipelineState, mods: Modules, agent) -> PipelineState:
    state.stage = "done"
    summary = summarise(state, mods.llm)
    agent(summary, step="summary")
    state.record("agent", "success", summary)
    return state
