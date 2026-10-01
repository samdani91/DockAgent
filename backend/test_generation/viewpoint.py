"""S3: Determine test viewpoints — commandTest vs fileExistenceTest per file."""

import logging
import posixpath
import subprocess
from typing import Callable, Optional

import docker
import docker.errors

from .data_structures import File

log = logging.getLogger("dockagent.test")

#: Abort rather than emit a misleading spec once this many probes have failed
#: for reasons unrelated to the file being checked.
MAX_PROBE_FAILURES = 10


class ViewpointDeterminer:
    def __init__(self, image_name: str, command_search_paths: list[str]):
        self._image_name = image_name
        self._search_paths = command_search_paths
        self._probe_failures = 0

    def determine(
        self,
        layers: list,
        progress: Callable[[str, str], None],
    ) -> tuple[list[tuple], list[tuple]]:
        command_test_infos: list[tuple] = []
        exist_test_infos: list[tuple] = []

        client = docker.from_env()
        container = _create_container(client, self._image_name)
        container_id = container.id
        try:
            for idx, layer in enumerate(layers):
                if layer.files:
                    progress(
                        "S3",
                        f"S3 — Checking layer {idx + 1}/{len(layers)}: {len(layer.files)} files…",
                    )

                binary_files: list[File] = []
                non_binary_files: list[File] = []

                resolved = self._resolve_batch(layer.files, container_id)
                for f in layer.files:
                    check_cmd = resolved.get(f.path)
                    if check_cmd is not None:
                        f.check_command = check_cmd
                        binary_files.append(f)
                    else:
                        non_binary_files.append(f)

                inst = layer.created_by.replace("'", " ").rstrip("\n")
                command_test_infos.append((binary_files, inst))
                exist_test_infos.append((non_binary_files, inst))
        finally:
            _stop_remove(container)

        return command_test_infos, exist_test_infos


    def _candidates(self, files: list[File]) -> dict[str, str]:
        """Files that could be commands, as {command name: expected path}.

        A name that maps to two different paths within one layer is dropped:
        one lookup cannot answer for both, and guessing would be wrong.
        """
        candidates: dict[str, str] = {}
        ambiguous: set[str] = set()
        for f in files:
            if f.permissions.startswith("c") or f.is_dir:
                continue
            if not any(f.path.startswith(sp) for sp in self._search_paths):
                continue
            name = posixpath.basename(f.path)
            if name in candidates and candidates[name] != f.path:
                ambiguous.add(name)
            candidates[name] = f.path
        for name in ambiguous:
            candidates.pop(name, None)
        return candidates

    def _resolve_batch(self, files: list[File], container_id: str) -> dict[str, str]:
        """Map file path -> resolver that found it ("which" or "type").

        One `docker exec` per resolver per layer instead of two per file. On a
        real image that is thousands of round trips saved; the comparison
        itself is unchanged.
        """
        candidates = self._candidates(files)
        if not candidates:
            return {}

        found: dict[str, str] = {}
        pending = dict(candidates)

        for check_cmd, template in (
            ("which", "which '{n}' 2>/dev/null || true"),
            ("type", "type -P '{n}' 2>/dev/null || true"),
        ):
            if not pending:
                break
            names = list(pending)
            # One line of output per name, in order, blank when unresolved.
            script = "; ".join(
                f"printf '%s\\n' \"$({template.format(n=n)})\"" for n in names
            )
            output = self._exec(container_id, script, len(names))
            if output is None:
                continue

            lines = output.split("\n")
            for name, line in zip(names, lines):
                if line.strip() and line.strip() == pending[name]:
                    found[pending[name]] = check_cmd
                    pending.pop(name, None)

        return found

    def _exec(self, container_id: str, script: str, count: int) -> Optional[str]:
        """Run *script* in the container, or None if the probe itself failed."""
        # Scales with the number of names resolved, not a flat 5s.
        timeout = min(300, 10 + count // 20)
        try:
            proc = subprocess.run(
                ["docker", "exec", container_id, "sh", "-c", script],
                capture_output=True, timeout=timeout, text=True,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            self._probe_failures += 1
            log.warning("batch probe failed (%s); %d failure(s) so far",
                        type(exc).__name__, self._probe_failures)
            if self._probe_failures >= MAX_PROBE_FAILURES:
                raise RuntimeError(
                    f"Gave up determining test viewpoints: "
                    f"{self._probe_failures} container probes failed. "
                    f"The generated tests would be unreliable."
                ) from exc
            return None
        return proc.stdout

def _create_container(client, image_name: str):
    c = client.containers.create(image_name, command="tail -f /dev/null", entrypoint="")
    c.start()
    return c


def _stop_remove(container) -> None:
    if container is None:
        return
    try:
        container.stop(timeout=3)
        container.remove()
    except Exception as exc:
        # Cleanup failure must not mask the real result, but a silently leaked
        # container is worth knowing about.
        log.warning("could not remove container %s: %s",
                    getattr(container, "short_id", "?"), exc)
