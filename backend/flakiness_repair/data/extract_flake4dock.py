"""Extract the FLAKIDOCK repair demonstrations into a compact retrieval corpus.

The published Flake4Dock dataset is ~7.3 GB — mostly full repository checkouts
and nine months of build logs per project. Only a small slice of it is useful
as RAG demonstrations: for the 100-odd Dockerfiles that ship with a repair, we
need the tuple the paper defines as (S_d, D_d, C_d, R_d, I_d).

This script pulls exactly that slice into one JSONL file, storing the repair as
a unified diff rather than a full Dockerfile (the fix is usually one or two
lines, so the diff is both smaller and a clearer demonstration).

Usage:
    python extract_flake4dock.py \
        --source ~/Desktop/docker-flakiness-study/dataset/flake4dock/possible-flaky-repos \
        --output flake4dock_repairs.jsonl
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
from pathlib import Path

# Keep records well inside embedding token limits.
MAX_DOCKERFILE_CHARS = 6000
MAX_ERROR_CHARS = 4000


def _read(path: Path, limit: int | None = None) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if limit and len(text) > limit:
        return text[:limit] + "\n… [truncated]"
    return text


def _parse_root_cause(path: Path) -> tuple[str, str, int]:
    """Return (label, description, rounds) from root_cause.txt."""
    text = _read(path)
    if not text:
        return "", "", 0

    label = ""
    rounds = 0
    m = re.search(r"^label:\s*(.+)$", text, re.MULTILINE)
    if m:
        label = m.group(1).strip()
    m = re.search(r"^rounds:\s*(\d+)", text, re.MULTILINE)
    if m:
        rounds = int(m.group(1))

    # Description sits between the two underscore rules.
    parts = text.split("__________")
    description = parts[1].strip() if len(parts) >= 2 else ""
    return label, description, rounds


def _parse_llm_summary(errors_dir: Path) -> dict:
    for candidate in sorted(errors_dir.glob("llm-summary-*.json")):
        try:
            return json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _repair_diff(original: str, repaired: str, name: str) -> str:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        repaired.splitlines(keepends=True),
        fromfile="Dockerfile",
        tofile=name,
        n=3,
    )
    return "".join(diff).strip()


def extract(source: Path) -> list[dict]:
    records: list[dict] = []
    skipped: list[str] = []

    for repo_dir in sorted(p for p in source.iterdir() if p.is_dir()):
        repairs = sorted(repo_dir.glob("repair*.Dockerfile"))
        if not repairs:
            continue

        dockerfile_path = repo_dir / "Dockerfile"
        if not dockerfile_path.is_file():
            skipped.append(f"{repo_dir.name}: no Dockerfile")
            continue

        original = _read(dockerfile_path)
        errors_dir = repo_dir / "unique-build-errors"
        error_logs = sorted(errors_dir.glob("*.log")) if errors_dir.is_dir() else []
        if not error_logs:
            skipped.append(f"{repo_dir.name}: no unique-build-errors")
            continue

        label, description, rounds = _parse_root_cause(repo_dir / "root_cause.txt")
        summary = _parse_llm_summary(errors_dir)

        diffs = []
        for repair_path in repairs:
            repaired = _read(repair_path)
            diff = _repair_diff(original, repaired, repair_path.name)
            if diff:
                diffs.append(diff)
        if not diffs:
            skipped.append(f"{repo_dir.name}: repair identical to original")
            continue

        # One record per distinct build error, sharing the repo's repairs.
        for error_log in error_logs:
            records.append({
                "id": f"{repo_dir.name}#{error_log.stem}",
                "project": repo_dir.name,
                "label": label,
                "description": description,
                "category": summary.get("category", ""),
                "summary": summary.get("summary", ""),
                "sources_of_error": summary.get("sources of error", []),
                "rounds": rounds,
                "dockerfile": original[:MAX_DOCKERFILE_CHARS],
                "error": _read(error_log, MAX_ERROR_CHARS),
                "repair_diffs": diffs,
            })

    if skipped:
        print(f"skipped {len(skipped)} repo(s):")
        for line in skipped:
            print(f"  - {line}")

    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True,
                        help="Path to flake4dock/possible-flaky-repos")
    parser.add_argument("--output", default="flake4dock_repairs.jsonl")
    args = parser.parse_args()

    source = Path(os.path.expanduser(args.source))
    if not source.is_dir():
        raise SystemExit(f"source not found: {source}")

    records = extract(source)
    out = Path(args.output)
    with out.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    labels: dict[str, int] = {}
    for r in records:
        labels[r["label"] or "(unlabelled)"] = labels.get(r["label"] or "(unlabelled)", 0) + 1

    print(f"\nwrote {len(records)} records to {out} "
          f"({out.stat().st_size / 1024:.0f} KB)")
    print(f"projects: {len({r['project'] for r in records})}")
    print("\nlabels:")
    for label, count in sorted(labels.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>3}  {label}")


if __name__ == "__main__":
    main()
