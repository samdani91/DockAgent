from dataclasses import dataclass, field


class MetadataElement:
    def __init__(self, type_, *, key=None, value):
        self.type = type_
        self.key = key
        self.value = value
        self.from_dfile = False
        self.from_ins = False
        self.points = 0

    def equal_value(self, other):
        def norm(v):
            if isinstance(v, list):
                v = str(v).replace("'", "")
            return v.replace(" ", "").replace('"', "")
        return norm(self.value) == norm(other.value)

    def equal_key(self, other):
        return self.key.replace(" ", "") == other.key.replace(" ", "")


class File:
    def __init__(self, path, is_file, is_dir, permissions):
        self.path = path
        self.is_file = is_file
        self.is_dir = is_dir
        self.permissions = permissions
        self.inst_points = 0
        self.path_points = 0
        self.check_command = None

    @property
    def points(self):
        return self.inst_points + self.path_points


class Layer:
    def __init__(self, created_by, comment, files, removed_paths):
        self.created_by = created_by
        self.comment = comment
        self.files = files
        self.removed_paths = removed_paths
        self.workdir = None
        self.inst_info = None

    def set_inst_info(self, inst_info):
        self.inst_info = inst_info


# ---------------------------------------------------------------------------
# S5 — test execution results
# ---------------------------------------------------------------------------

@dataclass
class TestCaseResult:
    name: str
    passed: bool
    errors: list[str] = field(default_factory=list)   # empty when passed


@dataclass
class TestRunResult:
    total: int
    passed: int
    failed: int
    results: list[TestCaseResult] = field(default_factory=list)
    raw_output: str = ""                              # kept for the UI detail panel


@dataclass
class PipelineResult:
    """What TestPipeline.run() hands back.

    The spec path is always present even when execution fails, so a runner
    problem never discards the generated YAML.
    """
    output_path: str
    test_run: TestRunResult | None = None
    execution_error: str | None = None
