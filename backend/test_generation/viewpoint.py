"""S3: Determine test viewpoints — commandTest vs fileExistenceTest per file."""

import posixpath
import subprocess
from typing import Callable, Optional

import docker
import docker.errors

from .data_structures import File


class ViewpointDeterminer:
    def __init__(self, image_name: str, command_search_paths: list[str]):
        self._image_name = image_name
        self._search_paths = command_search_paths

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

                for f in layer.files:
                    check_cmd = self._check_command(f, container_id)
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

    def _check_command(self, f: File, container_id: str) -> Optional[str]:
        if f.permissions.startswith("c") or f.is_dir:
            return None
        for sp in self._search_paths:
            if f.path.startswith(sp):
                cmd_name = posixpath.basename(f.path)
                for check_cmd, args in (
                    ("which", [cmd_name]),
                    ("type", ["-P", cmd_name]),
                ):
                    try:
                        proc = subprocess.run(
                            ["docker", "exec", container_id, check_cmd] + args,
                            capture_output=True,
                            timeout=5,
                            text=True,
                        )
                        if proc.returncode == 0 and proc.stdout.strip() == f.path:
                            return check_cmd
                    except subprocess.TimeoutExpired:
                        pass
                    except Exception:
                        pass
        return None


# ---------------------------------------------------------------------------
# Container lifecycle helpers
# ---------------------------------------------------------------------------

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
    except Exception:
        pass


def _recover(container, client, image_name: str):
    _stop_remove(container)
    return _create_container(client, image_name)
