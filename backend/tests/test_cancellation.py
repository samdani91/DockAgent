"""Cancellation reaches the active process and ends the SSE stream cleanly."""

import asyncio
import queue
import sys
import threading
import time

import pytest

from cancellation import RunCancelled, run_process
from main import _sse_stream
from test_generation.pipeline import TestPipeline


def test_cancel_before_process_start():
    stopped = threading.Event()
    stopped.set()

    with pytest.raises(RunCancelled):
        run_process(["command-that-must-not-run"], cancelled=stopped.is_set)


def test_cancel_interrupts_active_process():
    stopped = threading.Event()
    timer = threading.Timer(0.2, stopped.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(RunCancelled):
            run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cancelled=stopped.is_set,
                capture_output=True,
                timeout=30,
            )
    finally:
        timer.join()
    assert time.monotonic() - started < 3


def test_stream_close_signals_cancellation():
    stopped = threading.Event()
    events = queue.Queue()
    events.put({"step": "started"})

    async def close_stream():
        stream = _sse_stream(events, cancel=stopped)
        assert "started" in await anext(stream)
        await stream.aclose()

    asyncio.run(close_stream())
    assert stopped.is_set()


def test_test_generation_stops_after_build(tmp_path, monkeypatch):
    stopped = threading.Event()

    def build_then_stop(*_args, **_kwargs):
        stopped.set()

    monkeypatch.setattr("test_generation.pipeline.build_image", build_then_stop)
    with pytest.raises(RunCancelled):
        TestPipeline().run(
            dockerfile_path=str(tmp_path / "Dockerfile"),
            workspace_path=str(tmp_path),
            output_path=str(tmp_path / "test.yaml"),
            threshold=0,
            progress=lambda _step, _message: None,
            cancelled=stopped.is_set,
        )
