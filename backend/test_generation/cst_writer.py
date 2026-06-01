"""Serialise test results to Container Structure Test (CST) YAML format."""

import os
import posixpath
import re
from typing import Callable, Optional

from .data_structures import File, MetadataElement
from .expectation import ExpectationAcquirer, mask_patch_version


def write(
    filtered_test_targets: dict,
    info: dict,
    image_name: str,
    output_path: str,
    progress: Callable[[str, str], None],
) -> None:
    metadata: list[MetadataElement] = filtered_test_targets["metadata"]
    layers = filtered_test_targets["layers"]

    path_env = info["envs"].get(
        "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    command_search_paths = path_env.split(":")

    # S3 — determine viewpoints
    total_files = sum(len(l.files) for l in layers)
    progress("S3", f"S3 — Determining viewpoints for {total_files} files across {len(layers)} layers…")

    from .viewpoint import ViewpointDeterminer
    vd = ViewpointDeterminer(image_name, command_search_paths)
    command_test_infos, exist_test_infos = vd.determine(layers, progress)

    cmd_count = sum(len(f) for f, _ in command_test_infos)
    exist_count = sum(len(f) for f, _ in exist_test_infos)
    progress("S3", f"S3 — {cmd_count} command tests, {exist_count} file existence tests identified.")

    # S4 — acquire version expectations
    all_commands = [
        posixpath.basename(f.path)
        for files, _ in command_test_infos
        for f in files
    ]

    if all_commands:
        progress("S4", f"S4 — Acquiring version expectations for {len(all_commands)} commands…")
        acquirer = ExpectationAcquirer()
        versioned = acquirer.get_versions(all_commands, image_name, progress)
        progress("S4", f"S4 — Got version info for {len(versioned)}/{len(all_commands)} commands.")
    else:
        versioned = {}
        progress("S4", "S4 — No binary commands to version-check.")

    # Write YAML
    progress("writing", "Writing CST YAML file…")
    metadata_block = _generate_metadata_test(metadata)
    command_block = _generate_command_tests(command_test_infos, versioned)
    exist_block = _generate_file_existence_tests(exist_test_infos)

    counts = (
        f"# metadata: {len(metadata)}  "
        f"command: {cmd_count}  "
        f"exist: {exist_count}\n"
    )
    output = counts + "schemaVersion: '2.0.0'\n" + metadata_block + command_block + exist_block

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as fh:
        fh.write(output)


# ---------------------------------------------------------------------------
# Section generators
# ---------------------------------------------------------------------------

def _generate_metadata_test(metadata: list[MetadataElement]) -> str:
    envs    = [e for e in metadata if e.type == "envVars"]
    labels  = [e for e in metadata if e.type == "labels"]
    ports   = [e for e in metadata if e.type == "exposedPorts"]
    volumes = [e for e in metadata if e.type == "volumes"]
    others  = [e for e in metadata if e.type in ("entrypoint", "cmd", "workdir", "user")]

    lines: list[str] = []
    if envs:
        lines.append("  envVars:")
        for e in envs:
            lines.append(f'    - key: "{e.key}"')
            lines.append(f'      value: "{e.value}"')
    if labels:
        lines.append("  labels:")
        for e in labels:
            lines.append(f"    - key: {e.key}")
            lines.append(f"      value: {e.value}")
    if ports:
        lines.append("  exposedPorts: " + str([e.value for e in ports]))
    if volumes:
        lines.append("  volumes: " + str([e.value for e in volumes]))
    for e in others:
        lines.append(f"  {e.type}: {e.value}")

    if not lines:
        return ""
    return "metadataTest:\n" + "\n".join(lines) + "\n"


def _generate_command_tests(infos: list[tuple], versioned: dict) -> str:
    entries: list[str] = []
    for files, inst in infos:
        inst_clean = inst.rstrip("\n")
        for f in files:
            command = posixpath.basename(f.path)
            block: list[str] = [
                f"  - name: 'check {command}: {inst_clean}'",
            ]
            if f.check_command == "which":
                block += [f"    command: 'which'", f"    args: ['{command}']"]
            else:
                block += [f"    command: 'type'", f"    args: ['-P', '{command}']"]
            block.append(f"    expectedOutput: ['{re.escape(f.path)}']")
            entries.append("\n".join(block))

            if command in versioned:
                option, (stdout_v, stderr_v) = versioned[command]
                b2 = [
                    f"  - name: 'check {command} version: {inst_clean}'",
                    f"    command: '{command}'",
                    f"    args: ['{option}']",
                ]
                if stdout_v:
                    b2.append(f"    expectedOutput: ['{mask_patch_version(stdout_v)}']")
                elif stderr_v:
                    b2.append(f"    expectedError: ['{mask_patch_version(stderr_v)}']")
                entries.append("\n".join(b2))

    if not entries:
        return ""
    return "commandTests:\n" + "\n".join(entries) + "\n"


def _generate_file_existence_tests(infos: list[tuple]) -> str:
    entries: list[str] = []
    for files, inst in infos:
        inst_clean = inst.rstrip("\n")
        for f in files:
            should = "false" if f.permissions.startswith("c") else "true"
            entries.append(
                "\n".join([
                    f"  - name: '{f.path}: {inst_clean}'",
                    f"    path: '{f.path}'",
                    f"    shouldExist: {should}",
                ])
            )
    if not entries:
        return ""
    return "fileExistenceTests:\n" + "\n".join(entries) + "\n"
