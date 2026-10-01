"""S0: Build the Docker image, splitting RUN commands for finer layer granularity."""

import os
import re
import shlex
import subprocess
import tempfile


def build_image(dockerfile_path: str, workspace_path: str, image_name: str) -> None:
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
        result = subprocess.run(
            ["docker", "build", "-f", tmp_path, "-t", image_name, "."],
            cwd=workspace_path,
            capture_output=True,
            timeout=300,
        )
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
