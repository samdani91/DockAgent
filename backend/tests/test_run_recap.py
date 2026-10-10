"""summary_lines() is the chat's entire view of a run.

Whatever is missing here, the assistant cannot answer from — and with thin
context it fills the gap with plausible invention rather than saying nothing.
"""

from coordination.runners import to_test_outcome
from coordination.state import (
    FailingTest,
    FlakinessOutcome,
    GenerationOutcome,
    PipelineState,
    TestOutcome,
    _condense,
)
from test_generation.data_structures import (
    PipelineResult,
    TestCaseResult,
    TestRunResult,
)


def _state(**kw):
    return PipelineState(workspace_path="/w", dockerfile_path="/w/Dockerfile", **kw)


def _recap(**kw):
    return "\n".join(_state(**kw).summary_lines())


# ---------------------------------------------------------------------------
# Module 1
# ---------------------------------------------------------------------------

def test_a_failed_generation_carries_its_reason():
    """Regression: the recap said only "failed after 6 attempt(s)"."""
    recap = _recap(generation=GenerationOutcome(
        success=False, attempts=6,
        message="Could not produce a Dockerfile that builds after 6 attempt(s). "
                "Last error: ERROR: failed to solve: pip install failed"))

    assert "failed after 6 attempt(s)" in recap
    assert "pip install failed" in recap


def test_optimization_is_reported_with_its_saving():
    recap = _recap(generation=GenerationOutcome(
        success=True, attempts=1, optimized=True, size_reduction_pct=43.75))

    assert "multi-stage optimization applied (43.8% smaller)" in recap


def test_a_long_reason_is_bounded_but_keeps_the_tail():
    """A build log must not spend the whole prompt; the error is at the end."""
    message = "start " + ("noise " * 2000) + "ERROR: the actual cause"
    condensed = _condense(message)

    assert len(condensed) < 700
    assert condensed.startswith("start")
    assert "ERROR: the actual cause" in condensed
    assert "[…]" in condensed


# ---------------------------------------------------------------------------
# Module 2
# ---------------------------------------------------------------------------

def test_what_the_suite_covers_is_reported_not_just_the_count():
    recap = _recap(test=TestOutcome(
        executed=True, total=18, passed=18,
        command_tests=5, file_tests=12, metadata_tests=1))

    assert "18/18 passed" in recap
    assert "covering 5 command tests, 12 file existence tests, 1 metadata check." in recap


def test_a_single_test_is_named_so_it_can_be_explained():
    """The reported case: 1 of 1 passed left nothing to describe."""
    recap = _recap(test=TestOutcome(
        executed=True, total=1, passed=1, file_tests=1,
        passing=["entrypoint exists /app/run.sh"],
        spec_path="/w/.dockagent/tests/container-structure-test.yaml"))

    assert "covering 1 file existence test." in recap
    assert "passed: entrypoint exists /app/run.sh" in recap
    assert "container-structure-test.yaml" in recap


def test_the_passing_sample_is_capped():
    recap = _recap(test=TestOutcome(
        executed=True, total=40, passed=40,
        passing=[f"check {i}" for i in range(40)]))

    assert "check 11" in recap
    assert "check 12" not in recap
    assert "and 28 more" in recap


def test_coverage_is_reported_even_when_the_suite_could_not_run():
    recap = _recap(test=TestOutcome(
        executed=False, command_tests=3, file_tests=7,
        message="Container tests could not run: ERROR: failed to solve: boom"))

    assert "could not run" in recap
    assert "covering 3 command tests, 7 file existence tests." in recap


def test_the_mapping_carries_coverage_and_the_passing_names():
    cases = [TestCaseResult(name="npm exists", passed=True, errors=[]),
             TestCaseResult(name="node version", passed=False, errors=["got v18"])]
    outcome = to_test_outcome(PipelineResult(
        output_path="/w/spec.yaml",
        test_run=TestRunResult(total=2, passed=1, failed=1, results=cases),
        image_name="img-1", command_tests=2, file_tests=9, metadata_tests=1))

    assert outcome.image_name == "img-1"
    assert (outcome.command_tests, outcome.file_tests, outcome.metadata_tests) == (2, 9, 1)
    assert outcome.passing == ["npm exists"]
    assert [f.name for f in outcome.failing] == ["node version"]


# ---------------------------------------------------------------------------
# Module 3
# ---------------------------------------------------------------------------

def test_the_verdict_is_backed_by_the_build_counts():
    recap = _recap(flakiness=FlakinessOutcome(
        verdict="non-deterministic", is_flaky=True, needs_repair=True,
        iterations=2, successes=1, failures=1,
        failing_instruction="RUN apt-get update"))

    assert "1 of 2 no-cache builds succeeded, 1 failed." in recap
    assert "failing instruction: RUN apt-get update" in recap


def test_retrieved_fault_types_reach_the_recap():
    recap = _recap(flakiness=FlakinessOutcome(
        verdict="non-deterministic", needs_repair=True, repaired=True,
        attempts=2, applied=True, repaired_path="/w/Dockerfile",
        retrieved=["Repository Deprecation Fault", "Broken Link"]))

    assert "similar repairs retrieved: Repository Deprecation Fault, Broken Link" in recap
    assert "in 2 attempt(s) and applied to the Dockerfile" in recap


def test_an_unapplied_repair_says_where_it_went():
    recap = _recap(flakiness=FlakinessOutcome(
        verdict="non-deterministic", needs_repair=True, repaired=True,
        attempts=1, applied=False,
        repaired_path="/w/.dockagent/Dockerfile.repaired"))

    assert "written to /w/.dockagent/Dockerfile.repaired" in recap


def test_a_failed_repair_is_distinguished_from_no_repair_attempt():
    recap = _recap(flakiness=FlakinessOutcome(
        verdict="deterministic-failure", needs_repair=True,
        repaired=False, attempts=3))

    assert "no repair could be validated after 3 attempt(s)." in recap


def test_the_reported_error_reaches_the_recap():
    """The agent used to say "the log does not include the terminal output"."""
    recap = _recap(flakiness=FlakinessOutcome(
        verdict="deterministic-failure", needs_repair=True,
        iterations=2, successes=0, failures=2,
        failing_instruction="RUN apt-get update",
        stderr='process "/bin/sh -c apt-get update" did not complete '
               'successfully: exit code: 100',
        error_segment="#5 [2/2] RUN apt-get update\n"
                      "#5 Ign:1 http://deb.debian.org/debian stretch InRelease\n"
                      "#5 E: Failed to fetch http://deb.debian.org/... 404 Not Found"))

    assert "exit code: 100" in recap
    assert "E: Failed to fetch" in recap
    assert "404 Not Found" in recap


def test_the_doubled_buildkit_error_is_reported_once():
    recap = _recap(flakiness=FlakinessOutcome(
        verdict="deterministic-failure", needs_repair=True,
        stderr='process "/bin/sh -c apt-get update" did not complete successfully\n'
               'failed to build: failed to solve: process "/bin/sh -c apt-get '
               'update" did not complete successfully'))

    assert recap.count("did not complete successfully") == 1
    assert "failed to solve" in recap


def test_only_the_tail_of_the_failing_step_is_carried():
    """The raw log stays out; a step can print megabytes."""
    segment = "\n".join(["#5 [2/2] RUN build.sh"]
                        + [f"#5 progress line {i}" for i in range(400)]
                        + ["#5 error: the actual cause"])
    recap = _recap(flakiness=FlakinessOutcome(
        verdict="deterministic-failure", needs_repair=True, error_segment=segment))

    assert "error: the actual cause" in recap
    assert "progress line 0" not in recap
    assert len(recap) < 2500


# ---------------------------------------------------------------------------
# Whole run
# ---------------------------------------------------------------------------

def test_a_full_run_names_every_module_and_the_image():
    recap = _recap(
        image_name="dockagent-demo-1a2b",
        generation=GenerationOutcome(success=True, attempts=2),
        test=TestOutcome(executed=True, total=18, passed=16, failed=2,
                         command_tests=5, file_tests=12, metadata_tests=1,
                         failing=[FailingTest(name="node version", errors=["got v18"])]),
        flakiness=FlakinessOutcome(verdict="stable", iterations=2, successes=2),
        feedback_rounds=1)

    for expected in ("Dockerfile: /w/Dockerfile",
                     "Dockerfile generation: succeeded after 2 attempt(s).",
                     "Container tests: 16/18 passed.",
                     "failed: node version — got v18",
                     "Flakiness: stable.",
                     "2 of 2 no-cache builds succeeded, 0 failed.",
                     "Feedback rounds used: 1.",
                     "Image under test: dockagent-demo-1a2b"):
        assert expected in recap, expected
