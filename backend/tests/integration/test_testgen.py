"""Integration coverage for Module 2 — real images, real layers, real runner.

Covers report cases T8-T9.
"""

import re

import pytest
import yaml

from test_generation.enumerator import Enumerator
from test_generation.executor import execute_tests
from test_generation.pipeline import TestPipeline, _image_name_for
from test_generation.selector import Filter, Scorer

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_SIMPLE = """\
FROM node:20-alpine
WORKDIR /usr/src/app
COPY package.json ./
COPY server.js ./
ENV NODE_ENV=production
EXPOSE 3000
CMD ["node", "server.js"]
"""


@pytest.fixture
def image(undocumented_project, tagged_image):
    """A real image built from the Express fixture."""
    dockerfile = undocumented_project / "Dockerfile"
    dockerfile.write_text(_SIMPLE)
    tag = _image_name_for(str(undocumented_project))
    tagged_image(dockerfile, undocumented_project, tag)
    return tag, dockerfile, undocumented_project


# ---------------------------------------------------------------------------
# T8 — Layer inspection and target selection
# ---------------------------------------------------------------------------

def test_t8_targets_are_enumerated_from_a_real_image(image):
    tag, dockerfile, project = image
    targets, info = Enumerator().enumerate(str(dockerfile), tag, lambda s, m: None)

    assert targets["layers"], "no layers were enumerated"
    assert sum(len(l.files) for l in targets["layers"]) > 100, "too few files found"
    assert targets["metadata"], "no metadata enumerated"
    assert "envs" in info and info["envs"].get("PATH")

    # The image's own declarations should be visible among the metadata.
    values = " ".join(str(e.value) for e in targets["metadata"])
    assert "production" in values or "3000" in values


def test_t8_every_target_is_scored(image):
    tag, dockerfile, project = image
    targets, info = Enumerator().enumerate(str(dockerfile), tag, lambda s, m: None)
    scored = Scorer().score(targets, info)

    files = [f for l in scored["layers"] for f in l.files]
    assert files
    assert all(isinstance(f.points, int) for f in files)
    assert len({f.points for f in files}) > 1, "every file scored the same"


def test_t8_raising_the_threshold_selects_fewer_targets(image):
    tag, dockerfile, project = image
    targets, info = Enumerator().enumerate(str(dockerfile), tag, lambda s, m: None)
    scored = Scorer().score(targets, info)

    counts = []
    for threshold in (0, 5, 12, 20):
        kept = Filter().filter(scored, threshold)
        counts.append(sum(len(l.files) for l in kept["layers"]))

    assert counts == sorted(counts, reverse=True), f"not monotonic: {counts}"
    assert counts[0] > counts[-1], "the threshold had no effect"


# ---------------------------------------------------------------------------
# T9 — CST generation and execution
# ---------------------------------------------------------------------------

def test_t9_specification_is_valid_yaml_with_real_tests(image, tmp_path):
    tag, dockerfile, project = image
    out = tmp_path / "spec" / "container-structure-test.yaml"

    result = TestPipeline().run(
        dockerfile_path=str(dockerfile),
        workspace_path=str(project),
        output_path=str(out),
        threshold=10,
        progress=lambda s, m: None,
        execute=False,
    )

    assert out.is_file()
    spec = yaml.safe_load(out.read_text())
    assert spec["schemaVersion"] == "2.0.0"

    sections = [k for k in ("commandTests", "fileExistenceTests", "metadataTest")
                if k in spec]
    assert sections, "specification contains no tests at all"
    assert result.test_run is None          # execute=False


def test_t9_specification_is_accepted_and_executed_by_the_runner(image, tmp_path):
    """The framework must parse it and report per-test results."""
    tag, dockerfile, project = image
    out = tmp_path / "spec" / "container-structure-test.yaml"

    result = TestPipeline().run(
        dockerfile_path=str(dockerfile),
        workspace_path=str(project),
        output_path=str(out),
        threshold=10,
        progress=lambda s, m: None,
        execute=True,
    )

    assert result.test_run is not None, f"runner did not report: {result.execution_error}"
    run = result.test_run
    assert run.total > 0
    assert run.passed + run.failed == run.total
    assert len(run.results) == run.total
    assert all(isinstance(c.passed, bool) for c in run.results)
    # No parsing errors: the runner produced a report rather than refusing.
    assert run.raw_output.strip().startswith("{")


def test_t9_names_are_short_and_the_yaml_survives_quoting(image, tmp_path):
    """Regression: names used to embed the whole RUN instruction."""
    tag, dockerfile, project = image
    out = tmp_path / "spec" / "container-structure-test.yaml"

    TestPipeline().run(
        dockerfile_path=str(dockerfile), workspace_path=str(project),
        output_path=str(out), threshold=10,
        progress=lambda s, m: None, execute=False,
    )

    spec = yaml.safe_load(out.read_text())
    names = [t["name"] for section in ("commandTests", "fileExistenceTests")
             for t in spec.get(section, [])]
    assert names
    assert max(len(n) for n in names) < 120, "a test name is still enormous"
