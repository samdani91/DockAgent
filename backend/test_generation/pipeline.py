"""Orchestrate S0 → S1 → S2 → S3 → S4 with progress callbacks."""

import hashlib
import logging
import os
import re
from typing import Callable

from .builder import build_image
from .data_structures import PipelineResult
from .enumerator import Enumerator
from .executor import execute_tests
from .selector import Scorer, Filter
from .cst_writer import write as write_cst

log = logging.getLogger("dockagent.test")


def _image_name_for(workspace_path: str) -> str:
    digest = hashlib.md5(workspace_path.encode()).hexdigest()[:8]
    base = os.path.basename(workspace_path).lower()
    base = re.sub(r"[^a-z0-9]", "-", base).strip("-") or "app"
    return f"dockagent-{base}-{digest}"


class TestPipeline:
    def run(
        self,
        dockerfile_path: str,
        workspace_path: str,
        output_path: str,
        threshold: float,
        progress: Callable[[str, str], None],
        execute: bool = True,
        execute_timeout: int = 300,
    ) -> PipelineResult:
        import time

        image_name = _image_name_for(workspace_path)
        started = time.monotonic()

        def stage_done(stage: str) -> None:
            log.info("%s complete (%.1fs elapsed)", stage, time.monotonic() - started)

        # S0
        progress("S0", f"S0 — Building Docker image '{image_name}'…")
        build_image(dockerfile_path, workspace_path, image_name)
        progress("S0", "S0 — Docker image built successfully.")
        stage_done("S0 build")

        # S1
        progress("S1", "S1 — Saving image and extracting layers…")
        test_targets, info = Enumerator().enumerate(dockerfile_path, image_name, progress)

        file_count = sum(len(l.files) for l in test_targets["layers"])
        meta_count = len(test_targets["metadata"])
        progress("S1", f"S1 — Found {len(test_targets['layers'])} layers, {file_count} files, {meta_count} metadata elements.")
        stage_done("S1 enumerate")

        # S2
        progress("S2", f"S2 — Scoring {file_count} files…")
        scored = Scorer().score(test_targets, info)
        filtered = Filter().filter(scored, threshold)

        kept_files = sum(len(l.files) for l in filtered["layers"])
        kept_meta = len(filtered["metadata"])
        progress("S2", f"S2 — After filtering (threshold={threshold}): {kept_files} files, {kept_meta} metadata kept.")
        stage_done("S2 score+filter")

        # S3 + S4 + write (progress forwarded into cst_writer)
        write_cst(filtered, info, image_name, output_path, progress)
        stage_done("S3+S4 write")

        if not execute:
            return PipelineResult(output_path=output_path)

        # S5 — run the spec we just wrote.
        # A runner failure must not discard the YAML, so it is reported rather
        # than raised: the spec is a valid deliverable on its own.
        progress("S5", "S5 — Running container structure tests…")
        try:
            test_run = execute_tests(
                image_name, output_path, execute_timeout, progress
            )
        except RuntimeError as exc:
            progress("S5", f"S5 — Could not run tests: {exc}")
            return PipelineResult(
                output_path=output_path,
                execution_error=str(exc),
            )

        progress("S5", f"S5 — {test_run.passed}/{test_run.total} tests passed.")
        return PipelineResult(output_path=output_path, test_run=test_run)
