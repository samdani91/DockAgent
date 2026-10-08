"""Persist S5 execution results next to the generated spec.

The spec is reproducible; a run is not. Without this, a finished pipeline
leaves no record of what actually passed on which image at what time — the
results only ever existed in the SSE stream and the panel.

Reports are timestamped rather than overwritten so a sequence of runs (before
and after a repair, say) stays on disk and can be compared.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

from .data_structures import TestRunResult


def write_report(
    output_dir: str,
    image_name: str,
    spec_path: str,
    test_run: Optional[TestRunResult] = None,
    execution_error: Optional[str] = None,
    duration_seconds: Optional[float] = None,
) -> str:
    """Write one run's results as JSON and return the path.

    A runner failure is recorded too: "the suite could not be executed" is a
    result worth keeping, not an absence of one.
    """
    executed_at = datetime.now(timezone.utc)
    stamp = executed_at.strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(output_dir, f"results-{stamp}.json")

    payload: dict = {
        "image": image_name,
        "spec": spec_path,
        "executed_at": executed_at.isoformat(),
        "executed": test_run is not None,
    }
    if duration_seconds is not None:
        payload["duration_seconds"] = round(duration_seconds, 2)

    if test_run is not None:
        payload.update({
            "total": test_run.total,
            "passed": test_run.passed,
            "failed": test_run.failed,
            "cases": [
                {"name": c.name, "passed": c.passed, "errors": c.errors}
                for c in test_run.results
            ],
            "raw_output": test_run.raw_output,
        })
    else:
        payload["execution_error"] = execution_error or "unknown"

    os.makedirs(output_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")

    return path
