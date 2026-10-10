"""Reduce a Docker build log to the error features used for retrieval.

The paper's pre-processing step (Section III-B) splits a build output into
stages and keeps only the error-bearing parts. The published corpus stores the
result in a fixed section layout, and this module reproduces that layout
exactly — a retrieval query and the corpus records must share a representation
or cosine similarity compares apples to oranges.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

# `ERROR: failed to solve: process "/bin/sh -c <cmd>" did not complete successfully: exit code: N`
_FAILED_PROCESS = re.compile(
    r'process\s+"(?:/bin/sh\s+-c\s+)?(?P<cmd>.+?)"\s+did not complete successfully',
    re.DOTALL,
)
# BuildKit step headers come in two forms:
#   #12 [builder 3/12] RUN wget …
#    > [builder 3/12] RUN wget …:
_STEP_HEADER = re.compile(
    r"^\s*(?:#\d+\s+)?>?\s*\[(?P<stage>[^\]]+)\]\s+(?P<instruction>.+?)\s*$"
)
_ERROR_LINE = re.compile(r"^\s*(?:#\d+\s+)?ERROR:\s*(?P<body>.+)$", re.IGNORECASE)

# Base-image failures never name a RUN command — BuildKit reports them against
# its internal metadata step. These are the largest flakiness category in the
# corpus, so they must resolve back to the FROM line.
_IMAGE_REF = re.compile(
    r"(?:load metadata for|failed to resolve source metadata for|"
    r"pull access denied for|manifest for)\s+(?P<ref>[^\s:,\"']+(?::[^\s,\"']+)?)",
    re.IGNORECASE,
)

_MAX_SEGMENT_CHARS = 4000
#: Hard bound on lines held while scanning a step, so a step that prints
#: megabytes (a source compile) cannot be buffered in full.
_MAX_SEGMENT_LINES = 2000


@dataclass
class ErrorFeatures:
    """The error-bearing slice of a build log."""
    error_segment: str = ""            # output of the failing step
    dockerfile_error_line: str = ""    # the instruction that failed
    stderr: str = ""                   # the terminal ERROR: line(s)
    stage: str = ""                    # e.g. "builder 3/12"
    exit_code: int | None = None

    def as_query(self) -> str:
        """Render in the corpus' section layout, for embedding."""
        parts = []
        if self.error_segment:
            parts.append(f"** ERROR_SEGMENT **: \n{self.error_segment}")
        if self.dockerfile_error_line:
            parts.append(f"** DOCKERFILE_ERROR_LINE **: \n{self.dockerfile_error_line}")
        if self.stderr:
            parts.append(f"** STDERR **: \n{self.stderr}")
        return "\n\n".join(parts)

    def signature(self) -> str:
        """Stable key for deciding whether two failures are 'the same error'."""
        basis = self.stderr or self.error_segment or self.dockerfile_error_line
        collapsed = re.sub(r"\s+", " ", basis).strip().lower()
        return collapsed[:200]

    def __bool__(self) -> bool:
        return bool(self.error_segment or self.stderr or self.dockerfile_error_line)


def preprocess(log: str, dockerfile: str = "") -> ErrorFeatures:
    """Extract error features from *log*, resolving the instruction against
    *dockerfile* when the build output only shows a flattened command."""
    if not log or not log.strip():
        return ErrorFeatures()

    lines = log.splitlines()
    features = ErrorFeatures()

    # -- terminal ERROR lines (scanned in reverse; the last is the real one) --
    errors: list[str] = []
    for line in reversed(lines):
        m = _ERROR_LINE.match(line)
        if m:
            errors.append(m.group("body").strip())
            if len(errors) >= 3:
                break
    features.stderr = "\n".join(reversed(errors))

    # -- exit code --
    m = re.search(r"exit code:\s*(\d+)", log)
    if m:
        features.exit_code = int(m.group(1))

    # -- failing command, and the stage that ran it --
    m = _FAILED_PROCESS.search(log)
    failing_cmd = m.group("cmd").strip() if m else ""

    step_index = _find_failing_step(lines, failing_cmd)
    if step_index is not None:
        header = _STEP_HEADER.match(lines[step_index])
        if header:
            features.stage = header.group("stage").strip()
            # The " > [stage] RUN …:" form ends with a colon — drop it.
            features.dockerfile_error_line = header.group("instruction").rstrip(":").strip()
        features.error_segment = _segment_from(lines, step_index)

    # Prefer the Dockerfile's own formatting (line continuations intact).
    if dockerfile and failing_cmd:
        original = _match_instruction(dockerfile, failing_cmd)
        if original:
            features.dockerfile_error_line = original

    # A base-image failure has no RUN command; attribute it to its FROM line.
    if dockerfile and not failing_cmd:
        ref_match = _IMAGE_REF.search(log)
        if ref_match:
            from_line = _match_from_line(dockerfile, ref_match.group("ref"))
            if from_line:
                features.dockerfile_error_line = from_line

    if not features.error_segment and failing_cmd:
        features.error_segment = failing_cmd[:_MAX_SEGMENT_CHARS]

    return features


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _find_failing_step(lines: list[str], failing_cmd: str) -> int | None:
    """Index of the step header for the failing command, else the last header."""
    needle = _normalise(failing_cmd)[:80]
    last_header: int | None = None

    for i, line in enumerate(lines):
        if not _STEP_HEADER.match(line):
            continue
        last_header = i
        if needle and needle in _normalise(line):
            return i
    return last_header


def _segment_from(lines: list[str], start: int) -> str:
    """Collect the failing step's output, stopping at the next step or rule.

    Trimmed from the front, not the back. A step's output ends with the reason
    it failed — `E: Failed to fetch … 404`, a compiler's final error — while it
    begins with progress chatter: in a plain `apt-get update` failure 73% of
    the lines are `Ign:`/`Get:` noise. Cutting the head keeps what the retrieval
    query and the repair prompt are actually looking for.
    """
    header = lines[start].strip()
    body: deque[str] = deque(maxlen=_MAX_SEGMENT_LINES)
    seen = 0
    for line in lines[start + 1:]:
        if line.strip().startswith("------"):
            break
        if _STEP_HEADER.match(line):     # the next step began
            break
        body.append(line.rstrip())
        seen += 1

    kept = list(body)
    budget = _MAX_SEGMENT_CHARS - len(header)
    size = sum(len(line) + 1 for line in kept)
    dropped = 0
    while kept and size > budget:
        size -= len(kept.pop(0)) + 1
        dropped += 1
    dropped += seen - len(body)          # whatever the deque itself shed

    parts = [header]
    if dropped:
        parts.append(f"… [{dropped} earlier line(s) omitted]")
    parts.extend(kept)
    return "\n".join(parts).strip()


def _match_instruction(dockerfile: str, failing_cmd: str) -> str:
    """Find the Dockerfile instruction whose body matches *failing_cmd*.

    Build output flattens line continuations, so comparison is done on a
    whitespace-normalised form while the original text is what gets returned.
    """
    target = _normalise(failing_cmd)
    if not target:
        return ""

    for raw in _logical_lines(dockerfile):
        stripped = raw.strip()
        if not stripped.upper().startswith("RUN"):
            continue
        body = _normalise(stripped[3:])
        if body and (body == target or body[:80] == target[:80]):
            return stripped
    return ""


def _match_from_line(dockerfile: str, image_ref: str) -> str:
    """Find the FROM instruction that pulls *image_ref*.

    Build output reports fully-qualified references ("docker.io/library/node:16")
    while the Dockerfile usually writes the short form ("node:16"), so several
    candidate spellings are tried from most to least specific.
    """
    ref = image_ref.strip().lower()
    if not ref:
        return ""

    short = ref.rsplit("/", 1)[-1]              # docker.io/library/node:16 -> node:16
    candidates = [ref, short]
    # Repository without the tag, so "node:16" still matches "node:16.14.0".
    candidates += [c.split(":", 1)[0] for c in (ref, short)]

    for candidate in candidates:
        if not candidate:
            continue
        for raw in dockerfile.splitlines():
            stripped = raw.strip()
            if not stripped.upper().startswith("FROM"):
                continue
            if candidate in stripped[4:].strip().lower():
                return stripped
    return ""


def _logical_lines(dockerfile: str) -> list[str]:
    """Join backslash continuations into single logical instructions."""
    out: list[str] = []
    buf = ""
    for line in dockerfile.splitlines():
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1].rstrip() + " \\\n"
        else:
            out.append(buf + line)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _normalise(text: str) -> str:
    return re.sub(r"[\\\s]+", " ", text).strip().lower()
