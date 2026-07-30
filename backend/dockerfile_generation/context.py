"""Build the ProjectContext that feeds every LLM prompt.

Two sources are combined:
  1. User-provided build docs (README, INSTALL) — stripped of line-breaks per the paper.
  2. Auto-scanned project facts (entry point, port, framework, file tree) — structured text.

`build_context()` is the main entry point for the VS Code extension flow.
`read_docs()` is kept for the CLI and tests that pass doc paths explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .scanner import ProjectScan


@dataclass(frozen=True)
class ProjectContext:
    text: str       # full context for initial generation and context-driven repair
    scan_hint: str = ""  # compact critical facts prepended to error-driven repair prompts


def read_docs(doc_paths: list[str]) -> ProjectContext:
    """Read at most 2 doc files, strip line-breaks, return joined text.

    Line-breaks are replaced with spaces so the text flows inline when
    injected into a prompt (matching the paper's pre-processing step).
    """
    if len(doc_paths) > 2:
        doc_paths = doc_paths[:2]

    parts: list[str] = []
    for path_str in doc_paths:
        path = Path(path_str)
        if not path.exists():
            raise FileNotFoundError(f"Build doc not found: {path}")
        raw = path.read_text(encoding="utf-8", errors="replace")
        flattened = raw.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        parts.append(flattened.strip())

    return ProjectContext(text=" ".join(parts))


def build_context(
    doc_paths: list[str],
    repo_path: str | Path | None = None,
) -> ProjectContext:
    """Combine build docs + auto-scan into a single ProjectContext.

    This is the preferred entry point for the VS Code extension flow because it
    gives the LLM concrete, project-specific facts (port, entry point, etc.)
    rather than relying purely on prose documentation.
    """
    from .scanner import scan_project

    sections: list[str] = []

    # ── Build docs ─────────────────────────────────────────────────────────
    if doc_paths:
        doc_context = read_docs(doc_paths)
        if doc_context.text.strip():
            sections.append("## Build documentation\n" + doc_context.text)

    # ── Project scan ───────────────────────────────────────────────────────
    scan: ProjectScan | None = None
    if repo_path is not None:
        scan = scan_project(repo_path)
        scan_text = scan.as_text()
        if scan_text.strip():
            sections.append(scan_text)

    full_text = "\n\n".join(sections)
    hint = scan.as_hint() if scan else ""
    return ProjectContext(text=full_text, scan_hint=hint)
