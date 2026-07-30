"""Auto-scan a project directory to extract concrete build facts.

These facts are injected into every LLM prompt so the model never has to guess
the entry point, port, or start command — the three most common sources of
generated Dockerfile bugs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProjectScan:
    language: str = "unknown"
    framework: str | None = None
    entry_point: str | None = None
    start_command: str | None = None
    port: str | None = None
    node_version: str | None = None
    python_version: str | None = None
    package_manager: str | None = None
    file_tree: str = ""
    key_files: dict[str, str] = field(default_factory=dict)

    def as_text(self) -> str:
        """Full scan summary — used in the initial generation prompt."""
        lines: list[str] = []
        lines.append("## Auto-detected project facts")
        lines.append(f"Language/runtime : {self.language}")
        if self.framework:
            lines.append(f"Framework        : {self.framework}")
        if self.entry_point:
            lines.append(f"Entry point      : {self.entry_point}")
        if self.start_command:
            lines.append(f"Start command    : {self.start_command}")
        if self.port:
            lines.append(f"Application port : {self.port}")
        if self.node_version:
            lines.append(f"Node version     : {self.node_version}")
        if self.python_version:
            lines.append(f"Python version   : {self.python_version}")
        if self.package_manager:
            lines.append(f"Package manager  : {self.package_manager}")

        if self.file_tree:
            lines.append("\n## Project file tree (root level)")
            lines.append(self.file_tree)

        if self.key_files:
            lines.append("\n## Key project files")
            for name, content in self.key_files.items():
                lines.append(f"\n### {name}\n{content}")

        return "\n".join(lines)

    def as_hint(self) -> str:
        """Compact critical facts — prepended to repair prompts to prevent regressions."""
        facts: list[str] = []
        if self.entry_point:
            facts.append(f"Entry point  : {self.entry_point}")
        if self.start_command:
            facts.append(f"Start command: {self.start_command}")
        if self.port:
            facts.append(f"Port         : {self.port}")
        if not facts:
            return ""
        return (
            "Critical project facts — do not change these while fixing the error:\n"
            + "\n".join(f"  {f}" for f in facts)
            + "\n\n"
        )


def scan_project(repo_path: str | Path) -> ProjectScan:
    """Walk *repo_path* and return structured build facts."""
    root = Path(repo_path)
    scan = ProjectScan()

    # ── File tree (root level, non-hidden) ────────────────────────────────
    try:
        entries = sorted(root.iterdir())
        scan.file_tree = "\n".join(
            e.name + ("/" if e.is_dir() else "")
            for e in entries
            if not e.name.startswith(".")
        )
    except PermissionError:
        pass

    # ── Port from .env / .env.example ─────────────────────────────────────
    for env_name in (".env", ".env.example"):
        env_path = root / env_name
        if env_path.exists():
            _extract_port(env_path.read_text(errors="replace"), scan)
            if scan.port:
                break

    # ── Language-specific scanners ────────────────────────────────────────
    if (root / "package.json").exists():
        _scan_nodejs(root / "package.json", scan)
    elif (root / "requirements.txt").exists() or (root / "pyproject.toml").exists():
        _scan_python(root, scan)
    elif (root / "go.mod").exists():
        scan.language = "go"
        scan.package_manager = "go modules"
        scan.key_files["go.mod"] = (root / "go.mod").read_text()[:500]
    elif (root / "pom.xml").exists():
        scan.language = "java"
        scan.package_manager = "maven"
    elif any((root / f).exists() for f in ("build.gradle", "build.gradle.kts")):
        scan.language = "java"
        scan.package_manager = "gradle"
    elif (root / "Gemfile").exists():
        scan.language = "ruby"
        scan.package_manager = "bundler"
        scan.key_files["Gemfile"] = (root / "Gemfile").read_text()[:400]
    elif (root / "Cargo.toml").exists():
        scan.language = "rust"
        scan.package_manager = "cargo"

    return scan


# ── Private helpers ────────────────────────────────────────────────────────

def _extract_port(env_text: str, scan: ProjectScan) -> None:
    for line in env_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip().upper() == "PORT":
            scan.port = value.strip().strip("\"'")
            return


def _scan_nodejs(pkg_path: Path, scan: ProjectScan) -> None:
    scan.language = "node"
    scan.package_manager = "npm"

    try:
        pkg = json.loads(pkg_path.read_text())
    except (json.JSONDecodeError, OSError):
        scan.key_files["package.json"] = pkg_path.read_text()[:800]
        return

    # Entry point: prefer scripts.start, fall back to "main" field
    scan.entry_point = pkg.get("main", "index.js")
    scripts = pkg.get("scripts", {})
    if "start" in scripts:
        start_cmd = scripts["start"]
        scan.start_command = start_cmd
        # e.g. "node index.js" or "node src/server.js"
        tokens = start_cmd.split()
        if len(tokens) >= 2 and tokens[0] == "node":
            scan.entry_point = tokens[1]

    # Node version
    engines = pkg.get("engines", {})
    if "node" in engines:
        scan.node_version = engines["node"]
    nvmrc = pkg_path.parent / ".nvmrc"
    if nvmrc.exists():
        scan.node_version = nvmrc.read_text().strip()

    # Framework
    all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    if "express" in all_deps:
        scan.framework = "Express.js"
    elif "fastify" in all_deps:
        scan.framework = "Fastify"
    elif "koa" in all_deps:
        scan.framework = "Koa"
    elif "@hapi/hapi" in all_deps or "hapi" in all_deps:
        scan.framework = "Hapi"
    elif "next" in all_deps:
        scan.framework = "Next.js"
    elif "nuxt" in all_deps:
        scan.framework = "Nuxt.js"
    elif "nest" in all_deps or "@nestjs/core" in all_deps:
        scan.framework = "NestJS"

    # Package manager
    root = pkg_path.parent
    if (root / "yarn.lock").exists():
        scan.package_manager = "yarn"
    elif (root / "pnpm-lock.yaml").exists():
        scan.package_manager = "pnpm"

    scan.key_files["package.json"] = pkg_path.read_text()


def _scan_python(root: Path, scan: ProjectScan) -> None:
    scan.language = "python"
    scan.package_manager = "pip"

    req_path = root / "requirements.txt"
    if req_path.exists():
        content = req_path.read_text()
        scan.key_files["requirements.txt"] = content
        lower = content.lower()
        if "fastapi" in lower:
            scan.framework = "FastAPI"
            scan.entry_point = scan.entry_point or "main.py"
            scan.port = scan.port or "8000"
        elif "flask" in lower:
            scan.framework = "Flask"
            scan.port = scan.port or "5000"
        elif "django" in lower:
            scan.framework = "Django"
            scan.port = scan.port or "8000"

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        scan.key_files["pyproject.toml"] = pyproject.read_text()[:600]
        scan.package_manager = "pip / uv"

    for vf_name in (".python-version", "runtime.txt"):
        vf = root / vf_name
        if vf.exists():
            scan.python_version = vf.read_text().strip()
            break
