"""A command that does not answer in UTF-8 must not end the run.

S4 probes every command in the image for a version string. One binary
answering with a stray ISO-8859 byte raised UnicodeDecodeError out of
subprocess, and because that is a ValueError rather than an OSError nothing
caught it — a single bad probe aborted the whole coordinated pipeline.
"""

import subprocess
import sys

import pytest

from cancellation import run_process
from test_generation import expectation, viewpoint

#: Writes the exact byte from the reported traceback.
_BAD_BYTES = [
    sys.executable, "-c",
    r"import sys; sys.stdout.buffer.write(b'myprog 1.2 \xc8\xc8 build')",
]


def test_the_offending_byte_is_a_value_error_not_an_os_error():
    """Why the old `except OSError` could not have caught it."""
    assert issubclass(UnicodeDecodeError, ValueError)
    assert not issubclass(UnicodeDecodeError, OSError)


def test_strict_decoding_is_what_used_to_crash():
    with pytest.raises(UnicodeDecodeError):
        run_process(_BAD_BYTES, capture_output=True, timeout=30,
                    text=True, cancelled=lambda: False)


def test_replacing_bad_bytes_keeps_the_version_readable():
    proc = run_process(_BAD_BYTES, capture_output=True, timeout=30,
                       text=True, errors="replace", cancelled=lambda: False)

    assert proc.returncode == 0
    # The version is still there; only the undecodable bytes are substituted.
    assert "myprog 1.2" in proc.stdout
    assert "build" in proc.stdout


def test_a_version_probe_survives_a_non_utf8_reply(monkeypatch):
    """The real call path, with docker stubbed out."""
    recorded = {}

    def fake_run_process(command, **kwargs):
        recorded.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "myprog 1.2 � build", "")

    monkeypatch.setattr(expectation, "run_process", fake_run_process)
    result = expectation._run_subprocess("container", ["myprog", "--version"])

    assert recorded["errors"] == "replace", "the probe still decodes strictly"
    assert result is not None
    assert result[0] == 0


def test_a_probe_that_cannot_be_decoded_is_skipped_not_fatal(monkeypatch):
    """Belt and braces: even if a decode error escapes, one command is lost."""
    def boom(command, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xc8", 0, 1, "invalid continuation byte")

    monkeypatch.setattr(expectation, "run_process", boom)

    assert expectation._run_subprocess("container", ["myprog", "--version"]) is None


def test_a_batch_viewpoint_probe_is_skipped_not_fatal(monkeypatch):
    def boom(command, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xc8", 0, 1, "invalid continuation byte")

    monkeypatch.setattr(viewpoint, "run_process", boom)
    determiner = viewpoint.ViewpointDeterminer("img", ["/usr/bin"])

    assert determiner._exec("container", "echo hi", 1) is None
    assert determiner._probe_failures == 1
