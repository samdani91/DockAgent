"""Tests for flakiness pre-processing and detection (no Docker required)."""

import pytest

from dockerfile_generation.build import BuildResult, FakeDockerBuilder
from flakiness_repair.detector import (
    DETERMINISTIC_FAILURE,
    NON_DETERMINISTIC,
    STABLE,
    detect,
)
from flakiness_repair.preprocess import preprocess

# ---------------------------------------------------------------------------
# Fixtures — shaped like real BuildKit output
# ---------------------------------------------------------------------------

_DOCKERFILE = """FROM debian:bullseye-slim AS builder
WORKDIR /opt
RUN wget -q https://zlib.net/zlib-1.2.13.tar.gz \\
    && tar xvf zlib-1.2.13.tar.gz \\
    && cd zlib-1.2.13 \\
    && ./configure
COPY . .
CMD ["/app/run"]
"""

_FAILURE_LOG = """\
#8 [builder 2/5] WORKDIR /opt
#8 DONE 0.1s

#9 [builder 3/5] RUN wget -q https://zlib.net/zlib-1.2.13.tar.gz     && tar xvf zlib-1.2.13.tar.gz     && cd zlib-1.2.13     && ./configure
#9 0.412 wget: server returned error: HTTP/1.1 404 Not Found
#9 ERROR: process "/bin/sh -c wget -q https://zlib.net/zlib-1.2.13.tar.gz     && tar xvf zlib-1.2.13.tar.gz     && cd zlib-1.2.13     && ./configure" did not complete successfully: exit code: 8
------
 > [builder 3/5] RUN wget -q https://zlib.net/zlib-1.2.13.tar.gz     && tar xvf zlib-1.2.13.tar.gz     && cd zlib-1.2.13     && ./configure:
0.412 wget: server returned error: HTTP/1.1 404 Not Found
------
ERROR: failed to solve: process "/bin/sh -c wget -q https://zlib.net/zlib-1.2.13.tar.gz     && tar xvf zlib-1.2.13.tar.gz     && cd zlib-1.2.13     && ./configure" did not complete successfully: exit code: 8
"""

_TIMEOUT_LOG = """\
#12 [build 4/6] RUN yarn install --production
#12 30.11 error An unexpected error occurred: "https://registry.yarnpkg.com/@material-ui/icons/-/icons-4.11.3.tgz: ESOCKETTIMEDOUT".
#12 ERROR: process "/bin/sh -c yarn install --production" did not complete successfully: exit code: 1
------
ERROR: failed to solve: process "/bin/sh -c yarn install --production" did not complete successfully: exit code: 1
"""

# Base-image failures name no RUN command — BuildKit attributes them to its
# internal metadata step. Base-image faults are the largest category in the
# Flake4Dock corpus, so this path matters.
_BASE_IMAGE_LOG = """\
#2 [internal] load metadata for docker.io/library/debian:bullseye-slim
#2 ERROR: docker.io/library/debian:bullseye-slim: not found
------
 > [internal] load metadata for docker.io/library/debian:bullseye-slim:
------
ERROR: failed to solve: debian:bullseye-slim: docker.io/library/debian:bullseye-slim: not found
"""

_OK = BuildResult(success=True, exit_code=0, log="Successfully built", image_id="ok")


def _fail(log=_FAILURE_LOG):
    return BuildResult(success=False, exit_code=1, log=log)


# ---------------------------------------------------------------------------
# preprocess
# ---------------------------------------------------------------------------

def test_extracts_exit_code_and_stderr():
    f = preprocess(_FAILURE_LOG, _DOCKERFILE)
    assert f.exit_code == 8
    assert "did not complete successfully" in f.stderr


def test_extracts_failing_stage():
    f = preprocess(_FAILURE_LOG, _DOCKERFILE)
    assert f.stage == "builder 3/5"


def test_error_segment_contains_the_underlying_message():
    f = preprocess(_FAILURE_LOG, _DOCKERFILE)
    assert "404 Not Found" in f.error_segment


def test_resolves_instruction_back_to_dockerfile_formatting():
    """Build output flattens continuations; the Dockerfile form is preferable."""
    f = preprocess(_FAILURE_LOG, _DOCKERFILE)
    assert f.dockerfile_error_line.startswith("RUN wget")
    assert "\\" in f.dockerfile_error_line   # continuations preserved


def test_works_without_the_dockerfile():
    f = preprocess(_FAILURE_LOG)
    assert bool(f)
    assert "wget" in f.dockerfile_error_line


def test_query_uses_the_corpus_section_layout():
    """Query and corpus records must share a representation for retrieval."""
    q = preprocess(_FAILURE_LOG, _DOCKERFILE).as_query()
    assert "** ERROR_SEGMENT **:" in q
    assert "** DOCKERFILE_ERROR_LINE **:" in q
    assert "** STDERR **:" in q


def test_signature_is_stable_across_whitespace_noise():
    a = preprocess(_FAILURE_LOG, _DOCKERFILE).signature()
    b = preprocess(_FAILURE_LOG.replace("     ", "  "), _DOCKERFILE).signature()
    assert a == b


def test_different_errors_have_different_signatures():
    a = preprocess(_FAILURE_LOG, _DOCKERFILE).signature()
    b = preprocess(_TIMEOUT_LOG, _DOCKERFILE).signature()
    assert a != b


def test_base_image_failure_resolves_to_the_from_line():
    f = preprocess(_BASE_IMAGE_LOG, _DOCKERFILE)
    assert f.dockerfile_error_line.startswith("FROM debian:bullseye-slim")


def test_base_image_failure_still_reports_an_error_without_the_dockerfile():
    f = preprocess(_BASE_IMAGE_LOG)
    assert bool(f)
    assert "not found" in f.stderr


def test_base_image_failure_differs_from_a_run_failure():
    a = preprocess(_BASE_IMAGE_LOG, _DOCKERFILE).signature()
    b = preprocess(_FAILURE_LOG, _DOCKERFILE).signature()
    assert a != b


def test_empty_log_yields_falsy_features():
    assert not preprocess("")
    assert not preprocess("   \n  ")


def test_log_without_a_recognisable_error_is_handled():
    f = preprocess("#1 [1/2] FROM alpine\n#1 DONE 0.1s\n")
    assert f.exit_code is None


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------

def test_mixed_outcomes_are_flaky():
    builder = FakeDockerBuilder([_OK, _fail()])
    report = detect(_DOCKERFILE, "/repo", builder, iterations=2)

    assert report.verdict == NON_DETERMINISTIC
    assert report.is_flaky is True
    assert report.needs_repair is True
    assert (report.successes, report.failures) == (1, 1)


def test_consistent_failure_is_not_reported_as_flaky():
    builder = FakeDockerBuilder([_fail()])
    report = detect(_DOCKERFILE, "/repo", builder, iterations=2)

    assert report.verdict == DETERMINISTIC_FAILURE
    assert report.is_flaky is False        # not observed instability
    assert report.needs_repair is True     # still worth repairing


def test_all_success_is_stable_but_not_a_guarantee():
    builder = FakeDockerBuilder([_OK])
    report = detect(_DOCKERFILE, "/repo", builder, iterations=2)

    assert report.verdict == STABLE
    assert report.needs_repair is False
    assert "not proof" in report.summary()


def test_builds_exactly_n_times():
    builder = FakeDockerBuilder([_OK])
    detect(_DOCKERFILE, "/repo", builder, iterations=4)
    assert len(builder.calls) == 4


def test_primary_error_is_the_first_failure():
    builder = FakeDockerBuilder([_OK, _fail(_TIMEOUT_LOG)])
    report = detect(_DOCKERFILE, "/repo", builder, iterations=2)
    assert report.primary_error is not None
    assert "yarn" in report.primary_error.dockerfile_error_line


def test_distinct_errors_are_deduplicated():
    builder = FakeDockerBuilder([_fail(), _fail(), _fail()])
    report = detect(_DOCKERFILE, "/repo", builder, iterations=3)
    assert len(report.distinct_errors) == 1


def test_two_different_failures_are_both_recorded():
    builder = FakeDockerBuilder([_fail(_FAILURE_LOG), _fail(_TIMEOUT_LOG)])
    report = detect(_DOCKERFILE, "/repo", builder, iterations=2)
    assert len(report.distinct_errors) == 2


def test_progress_is_reported_per_build():
    seen: list[str] = []
    builder = FakeDockerBuilder([_OK])
    detect(_DOCKERFILE, "/repo", builder, iterations=2,
           progress=lambda s, m: seen.append(m))
    assert sum("Build 1 of 2" in m for m in seen) >= 1
    assert sum("Build 2 of 2" in m for m in seen) >= 1


def test_zero_iterations_rejected():
    with pytest.raises(ValueError):
        detect(_DOCKERFILE, "/repo", FakeDockerBuilder([_OK]), iterations=0)


def test_the_segment_keeps_its_tail_not_its_head():
    """A step's output ends with the reason it failed.

    Trimming from the back dropped the `E: Failed to fetch … 404` lines that
    the retrieval query and the repair prompt are looking for, and kept the
    `Ign:` progress chatter instead.
    """
    from flakiness_repair.preprocess import _MAX_SEGMENT_CHARS, preprocess

    noise = "\n".join(
        f"#5 0.{i:03d} Ign:{i} http://deb.debian.org/debian stretch InRelease"
        for i in range(200)
    )
    log = (
        "#5 [2/2] RUN apt-get update\n"
        + noise
        + "\n#5 9.999 E: Failed to fetch http://deb.debian.org/x 404 Not Found\n"
        '#5 ERROR: process "/bin/sh -c apt-get update" did not complete '
        "successfully: exit code: 100\n"
    )
    features = preprocess(log, "FROM debian:stretch\nRUN apt-get update\n")

    assert len(log) > _MAX_SEGMENT_CHARS, "fixture must exceed the cap to be a test"
    assert "E: Failed to fetch" in features.error_segment
    assert "404 Not Found" in features.error_segment
    # The header stays for context, and the dropped head is accounted for.
    assert features.error_segment.splitlines()[0].startswith("#5 [2/2] RUN apt-get")
    assert "earlier line(s) omitted" in features.error_segment
    assert "Ign:0 " not in features.error_segment
