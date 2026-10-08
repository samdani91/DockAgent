import json
from pathlib import Path

from test_generation.data_structures import TestCaseResult, TestRunResult
from test_generation.report import write_report


def _run():
    return TestRunResult(
        total=3,
        passed=2,
        failed=1,
        results=[
            TestCaseResult(name="check python", passed=True),
            TestCaseResult(name="check node", passed=True),
            TestCaseResult(name="/app/main.py exists", passed=False,
                           errors=["file does not exist"]),
        ],
        raw_output='{"Pass": 2, "Fail": 1, "Total": 3}',
    )


def test_writes_executed_run(tmp_path):
    path = write_report(
        output_dir=str(tmp_path),
        image_name="img:latest",
        spec_path=str(tmp_path / "spec.yaml"),
        test_run=_run(),
        duration_seconds=12.345,
    )

    payload = json.loads(Path(path).read_text())
    assert payload["executed"] is True
    assert (payload["total"], payload["passed"], payload["failed"]) == (3, 2, 1)
    assert payload["duration_seconds"] == 12.35          # rounded to 2dp
    assert payload["image"] == "img:latest"
    assert len(payload["cases"]) == 3
    failing = [c for c in payload["cases"] if not c["passed"]]
    assert failing[0]["errors"] == ["file does not exist"]
    assert payload["raw_output"]


def test_records_a_runner_failure(tmp_path):
    """A suite that could not run is a result, not an absence of one."""
    path = write_report(
        output_dir=str(tmp_path),
        image_name="img:latest",
        spec_path=str(tmp_path / "spec.yaml"),
        execution_error="timed out after 300 seconds",
    )

    payload = json.loads(Path(path).read_text())
    assert payload["executed"] is False
    assert payload["execution_error"] == "timed out after 300 seconds"
    assert "cases" not in payload


def test_creates_the_directory(tmp_path):
    nested = tmp_path / "a" / "b"
    path = write_report(
        output_dir=str(nested),
        image_name="img:latest",
        spec_path="spec.yaml",
        test_run=_run(),
    )
    assert Path(path).is_file()


def test_runs_do_not_overwrite_each_other(tmp_path, monkeypatch):
    """Timestamped names keep a before/after pair on disk."""
    import test_generation.report as report

    stamps = iter(["20260101T000000Z", "20260101T000001Z"])
    real_datetime = report.datetime      # captured before it is shadowed

    class FrozenClock:
        @staticmethod
        def now(tz=None):
            value = next(stamps)
            parsed = real_datetime.strptime(value, "%Y%m%dT%H%M%SZ")
            return parsed.replace(tzinfo=report.timezone.utc)

    monkeypatch.setattr(report, "datetime", FrozenClock)

    first = write_report(str(tmp_path), "img", "spec.yaml", test_run=_run())
    second = write_report(str(tmp_path), "img", "spec.yaml", test_run=_run())

    assert first != second
    assert len(list(tmp_path.glob("results-*.json"))) == 2
