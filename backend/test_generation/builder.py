"""S0: Build the Docker image, splitting RUN commands for finer layer granularity."""

import os
import re
import shlex
import subprocess
import tempfile
from typing import Callable

from cancellation import run_process

#: How long a single S0 build may take.
#:
#: This was 300s, an unexamined default from the first version of the pipeline
#: that no project hit until a real one did: GloVe-experiments downloads an
#: 822 MB corpus and compiles scipy from source on Python 3.7, which needs
#: 15-25 minutes. Unlike Module 1, S0 gets one build and the module dies if it
#: fails, so the ceiling is set generously rather than tightly.
BUILD_TIMEOUT_SECONDS = 1800


def build_image(
    dockerfile_path: str,
    workspace_path: str,
    image_name: str,
    timeout: int = BUILD_TIMEOUT_SECONDS,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    with open(dockerfile_path, "r") as f:
        original = f.read()

    modified = split_run_commands(original)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".dockerfile",
        dir=workspace_path,
        delete=False,
        prefix=".dockagent_build_",
    ) as tf:
        tf.write(modified)
        tmp_path = tf.name

    try:
        try:
            result = run_process(
                ["docker", "build", "-f", tmp_path, "-t", image_name, "."],
                cwd=workspace_path,
                capture_output=True,
                timeout=timeout,
                cancelled=cancelled,
            )
        except subprocess.TimeoutExpired:
            # Raised as a RuntimeError like every other build failure, so the
            # caller reports it rather than leaking a subprocess traceback.
            spent = (
                f"{timeout // 60} minutes" if timeout >= 60
                else f"{timeout} seconds"
            )
            raise RuntimeError(
                f"docker build timed out after {spent}. "
                f"This project may need longer than the current limit allows."
            ) from None

        if result.returncode != 0:
            raise RuntimeError(
                f"docker build failed (exit {result.returncode}):\n"
                + result.stderr.decode(errors="replace")
            )
    finally:
        os.unlink(tmp_path)


def split_run_commands(content: str) -> str:
    """Rewrite a Dockerfile splitting compound RUN a && b into separate RUN layers."""
    lines = content.splitlines()
    # Strip comment-only lines
    filtered = [ln for ln in lines if not ln.strip().startswith("#")]

    # Join continuation lines
    joined: list[str] = []
    buf = ""
    for ln in filtered:
        stripped = ln.strip()
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
        else:
            buf += stripped
            joined.append(buf)
            buf = ""

    result: list[str] = []
    for ln in joined:
        if ln.strip().upper().startswith("RUN"):
            command = ln[3:].strip()
            result.extend(_split_run_value(command))
        else:
            result.append(ln)

    return "\n".join(result)


def _split_run_value(command: str) -> list[str]:
    """Split a RUN value on && separators, producing individual RUN lines."""
    # Strip leading --mount/--network/--security options
    options: list[str] = []
    remainder = command.strip()
    while remainder.startswith("--"):
        try:
            tokens = shlex.split(remainder, posix=True)
        except ValueError:
            tokens = remainder.split()
        if tokens and tokens[0].startswith("--"):
            options.append(tokens[0])
            remainder = remainder[len(tokens[0]):].lstrip()
        else:
            break

    parts = _split_on_and_and(remainder)
    if len(parts) <= 1:
        # `remainder`, not `command`: the options were sliced off the front of
        # `command`, so reusing it here emitted each option twice.
        return ["RUN " + " ".join(options + [remainder]).strip()]

    prefix = " ".join(options) + " " if options else ""
    result: list[str] = []
    carry: list[str] = []  # variable assignments / cd to propagate

    for part in parts:
        part = part.strip()
        if not part:
            continue
        add_prefix = ""
        if carry:
            add_prefix = " && \\\n    ".join(carry) + " && \\\n    "
        result.append(f"RUN {prefix}{add_prefix}{part}")

        # Carry variable assignments and cd/set to subsequent commands
        if _is_var_assignment(part):
            carry.append(part)
        else:
            try:
                tok = shlex.split(part)
                cmd = tok[0] if tok else ""
            except ValueError:
                cmd = part.split()[0] if part.split() else ""
            if cmd in ("cd", "set"):
                carry.append(part)

    return result if result else ["RUN " + command]


def _split_on_and_and(s: str) -> list[str]:
    """Split a shell command string on && respecting quotes."""
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "'" and not in_double:
            in_single = not in_single
            current.append(c)
        elif c == '"' and not in_single:
            in_double = not in_double
            current.append(c)
        elif c == "&" and not in_single and not in_double:
            if i + 1 < len(s) and s[i + 1] == "&":
                parts.append("".join(current).strip())
                current = []
                i += 2
                continue
            else:
                current.append(c)
        else:
            current.append(c)
        i += 1
    if current:
        parts.append("".join(current).strip())
    return parts


def _is_var_assignment(cmd: str) -> bool:
    return bool(re.match(r"^\s*[a-zA-Z_][a-zA-Z0-9_]*\s*=", cmd))
