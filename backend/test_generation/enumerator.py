"""S1: Enumerate test targets from Docker image layers and Dockerfile metadata."""

import io
import json
import os
import posixpath
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
from typing import Optional

from .data_structures import File, Layer, MetadataElement

_DIC_TYPES = ("envVars", "labels")
_LIST_TYPES = ("exposedPorts", "volumes")
_STR_TYPES = ("entrypoint", "cmd", "workdir", "user")


# ---------------------------------------------------------------------------
# Minimal Dockerfile parser
# ---------------------------------------------------------------------------

class DockerfileInfo:
    """Parse a Dockerfile and expose instruction metadata."""

    def __init__(self, content: str):
        self._content = content
        self._raw_structure = self._parse()

    @classmethod
    def from_path(cls, path: str) -> "DockerfileInfo":
        with open(path, "r", errors="replace") as f:
            return cls(f.read())

    # -- low-level parsing --

    def _parse(self) -> list[dict]:
        lines = self._content.splitlines()
        instructions: list[dict] = []
        i = 0
        while i < len(lines):
            raw = lines[i]
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            startline = i
            buf = stripped
            while buf.endswith("\\"):
                buf = buf[:-1] + " "
                i += 1
                while i < len(lines) and lines[i].strip().startswith("#"):
                    i += 1
                if i < len(lines):
                    buf += lines[i].strip()
            endline = i
            i += 1
            parts = buf.split(None, 1)
            if not parts:
                continue
            instr = parts[0].upper()
            value = parts[1].strip() if len(parts) > 1 else ""
            instructions.append({
                "instruction": instr,
                "value": value,
                "content": buf,
                "startline": startline,
                "endline": endline,
            })
        return instructions

    @property
    def structure(self) -> list[dict]:
        return self._raw_structure

    @property
    def no_var_structure(self) -> list[dict]:
        """Return structure with ENV/ARG variables substituted."""
        result = []
        envs: dict[str, str] = {}
        args: dict[str, str] = {}
        for desc in self._raw_structure:
            instr = desc["instruction"]
            value = self._substitute(desc["value"], envs, args)
            new_desc = dict(desc)
            new_desc["value"] = value
            new_desc["content"] = instr + " " + value
            if instr == "FROM":
                envs = {}
                args = {}
            elif instr == "ENV":
                for k, v in self._parse_env_arg(value, envs, args):
                    envs[k] = v
            elif instr == "ARG":
                for k, v in self._parse_env_arg(value, envs, args):
                    args[k] = v
            result.append(new_desc)
        return result

    def _substitute(self, text: str, envs: dict, args: dict) -> str:
        def rep(m: re.Match) -> str:
            name = m.group(1) or m.group(2)
            return envs.get(name, args.get(name, m.group(0)))
        return re.sub(r"\$\{([^}]+)\}|\$([a-zA-Z_][a-zA-Z0-9_]*)", rep, text)

    def _parse_env_arg(self, value: str, envs: dict, args: dict) -> list[tuple]:
        try:
            tokens = shlex.split(value)
        except ValueError:
            tokens = value.split()
        result = []
        if tokens and "=" not in tokens[0]:
            # ENV KEY value or ARG KEY=default form without key=
            if len(tokens) >= 2:
                result.append((tokens[0], " ".join(tokens[1:])))
            else:
                result.append((tokens[0], ""))
        else:
            for tok in tokens:
                if "=" in tok:
                    k, _, v = tok.partition("=")
                    result.append((k, v))
                else:
                    result.append((tok, ""))
        return result

    # -- metadata properties --

    @property
    def envs(self) -> dict:
        result: dict[str, str] = {}
        for d in self.no_var_structure:
            if d["instruction"] == "FROM":
                result = {}
            elif d["instruction"] == "ENV":
                for k, v in self._parse_env_arg(d["value"], result, {}):
                    result[k] = v
        return result

    @property
    def labels(self) -> dict:
        result: dict[str, str] = {}
        for d in self.no_var_structure:
            if d["instruction"] == "FROM":
                result = {}
            elif d["instruction"] == "LABEL":
                try:
                    tokens = shlex.split(d["value"])
                except ValueError:
                    tokens = d["value"].split()
                for tok in tokens:
                    if "=" in tok:
                        k, _, v = tok.partition("=")
                        result[k.strip('"')] = v.strip('"')
        return result

    @property
    def exposed_ports(self) -> list:
        result: list = []
        for d in self.no_var_structure:
            if d["instruction"] == "FROM":
                result = []
            elif d["instruction"] == "EXPOSE":
                result.append(d["value"])
        return result

    @property
    def volumes(self) -> list:
        result: list = []
        for d in self.no_var_structure:
            if d["instruction"] == "FROM":
                result = []
            elif d["instruction"] == "VOLUME":
                v = d["value"].strip()
                if v.startswith("["):
                    try:
                        result += json.loads(v)
                    except json.JSONDecodeError:
                        result.append(v)
                else:
                    result += v.split()
        return result

    @property
    def entrypoint(self) -> Optional[str]:
        result = None
        for d in self.structure:
            if d["instruction"] == "FROM":
                result = None
            elif d["instruction"] == "ENTRYPOINT":
                result = d["value"]
        return result

    @property
    def cmd(self) -> Optional[str]:
        result = None
        for d in self.structure:
            if d["instruction"] == "FROM":
                result = None
            elif d["instruction"] == "CMD":
                result = d["value"]
        return result

    @property
    def workdir(self) -> Optional[str]:
        result = None
        count = 0
        for d in self.no_var_structure:
            if d["instruction"] == "FROM":
                result = "/"
            elif d["instruction"] == "WORKDIR":
                count += 1
                v = d["value"]
                if posixpath.isabs(v):
                    result = v
                else:
                    result = posixpath.join(result or "/", v)
        return result if count > 0 else None

    @property
    def user(self) -> Optional[str]:
        result = None
        for d in self.no_var_structure:
            if d["instruction"] == "FROM":
                result = None
            elif d["instruction"] == "USER":
                result = d["value"]
        return result

    def get_now_workdir(self, startline: int) -> str:
        idx = next(
            (i for i, d in enumerate(self.no_var_structure) if d["startline"] == startline),
            None,
        )
        value = "/"
        for d in (self.no_var_structure[:idx + 1] if idx is not None else []):
            if d["instruction"] == "FROM":
                value = "/"
            elif d["instruction"] == "WORKDIR":
                v = d["value"]
                value = v if posixpath.isabs(v) else posixpath.join(value, v)
        return value


# ---------------------------------------------------------------------------
# Docker inspect wrapper
# ---------------------------------------------------------------------------

class DockerInspectData:
    def __init__(self, image_name: str):
        result = subprocess.run(
            ["docker", "inspect", image_name],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"docker inspect failed: {result.stderr.decode(errors='replace')}"
            )
        data = json.loads(result.stdout)
        self._cfg = data[0]["Config"]
        self._inspect = data[0]

    @property
    def envs(self) -> dict:
        env_list = self._cfg.get("Env") or []
        return dict(e.split("=", 1) for e in env_list if "=" in e)

    @property
    def labels(self) -> dict:
        return self._cfg.get("Labels") or {}

    @property
    def volumes(self) -> list:
        v = self._cfg.get("Volumes")
        return list(v.keys()) if v else []

    @property
    def entrypoint(self) -> Optional[str]:
        v = self._cfg.get("Entrypoint")
        return str(v) if v else None

    @property
    def cmd(self) -> str:
        v = self._cfg.get("Cmd")
        return str(v) if v is not None else str([])

    @property
    def workdir(self) -> Optional[str]:
        v = self._cfg.get("WorkingDir", "")
        return v if v else None

    @property
    def user(self) -> Optional[str]:
        v = self._cfg.get("User", "")
        return v if v else None


# ---------------------------------------------------------------------------
# Layer extraction from docker save
# ---------------------------------------------------------------------------

def _normalize_path(name: str) -> str:
    """Convert tar member name to absolute POSIX path."""
    if name in (".", "./"):
        return "/"
    # Only the leading "./" is a prefix; lstrip(".") also ate the dot of a
    # dotfile, turning "./.bashrc" into "/bashrc".
    p = name[2:] if name.startswith("./") else name
    if not p.startswith("/"):
        p = "/" + p
    return p


def _extract_layers_from_image(image_name: str) -> tuple[list[dict], list[dict]]:
    """
    Run docker save and extract layers via tarfile.
    Returns (history_entries, layer_file_lists) where layer_file_lists is a
    list of (files: list[File], removed_paths: list[str]) per non-empty history entry.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        image_tar = os.path.join(tmpdir, "image.tar")
        result = subprocess.run(
            ["docker", "save", image_name, "-o", image_tar],
            capture_output=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"docker save failed: {result.stderr.decode(errors='replace')}"
            )

        with tarfile.open(image_tar, "r") as outer:
            manifest_raw = outer.extractfile("manifest.json")
            manifest = json.loads(manifest_raw.read())

            config_path = manifest[0]["Config"]
            config_raw = outer.extractfile(config_path)
            config = json.loads(config_raw.read())
            history: list[dict] = config.get("history", [])

            layer_paths: list[str] = manifest[0]["Layers"]

            per_layer_data: list[tuple[list[File], list[str]]] = []
            for lpath in layer_paths:
                layer_data = outer.extractfile(lpath)
                files: list[File] = []
                removed: list[str] = []

                # Streamed ("r|*") rather than read() into a BytesIO: a layer of
                # a multi-GB image used to be held in memory in full, and
                # getmembers() materialised the whole member list on top of it.
                with tarfile.open(fileobj=layer_data, mode="r|*") as ltf:
                    for member in ltf:
                        basename = posixpath.basename(member.name)
                        norm_path = _normalize_path(member.name)

                        if basename == ".wh..wh..opq":
                            # Opaque whiteout: entire parent directory deleted
                            parent = posixpath.dirname(norm_path)
                            removed.append(parent)
                        elif basename.startswith(".wh."):
                            # Individual whiteout
                            actual_name = basename[4:]
                            parent = posixpath.dirname(norm_path)
                            actual_path = posixpath.join(parent, actual_name)
                            removed.append(actual_path)
                            files.append(File(actual_path, False, False, "c---------"))
                        else:
                            permissions = stat.filemode(member.mode)
                            files.append(
                                File(
                                    norm_path,
                                    member.isfile(),
                                    member.isdir(),
                                    permissions,
                                )
                            )

                per_layer_data.append((files, removed))

    return history, per_layer_data


# ---------------------------------------------------------------------------
# Instruction analysis helpers
# ---------------------------------------------------------------------------

def _analyze_instruction(instruction: str, workdir: Optional[str]) -> dict:
    info = {
        "from": False,
        "from_keywords": [],
        "add": False,
        "copy": False,
        "dests": [],
        "run": False,
        "run_commands": [],
    }
    dfi = DockerfileInfo(instruction)
    struct = dfi.structure
    if not struct:
        return info
    first = struct[0]
    instr_name = first["instruction"]
    if instr_name == "FROM":
        info["from"] = True
        info["from_keywords"] = _analyze_from(first["value"])
    elif instr_name == "ADD":
        info["add"] = True
        info["dests"] = _analyze_copy_add(first["value"], workdir)
    elif instr_name == "COPY":
        info["copy"] = True
        info["dests"] = _analyze_copy_add(first["value"], workdir, check_parents=True)
    elif instr_name == "RUN":
        info["run"] = True
        info["run_commands"] = _analyze_run(first["value"])
    return info


def _analyze_from(value: str) -> list[str]:
    m = re.match(
        r"^([a-z0-9._\-/]+)(?::[\w.\-]+)?(?:\s+as\s+\S+)?$",
        value.strip(),
        re.IGNORECASE,
    )
    if m:
        base = m.group(1).split("/")[-1]
        keywords = re.split(r"[.\-_]", base)
        if "jdk" in value or "jre" in value:
            keywords.append("java")
        return [k for k in keywords if k]
    return []


def _analyze_copy_add(value: str, workdir: Optional[str], check_parents: bool = False) -> list[str]:
    """Extract destination paths from COPY/ADD value."""
    # Strip --option flags
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = value.split()

    options: list[str] = []
    i = 0
    while i < len(tokens) and tokens[i].startswith("--"):
        options.append(tokens[i])
        i += 1
    tokens = tokens[i:]

    if len(tokens) < 2:
        return []
    srcs = tokens[:-1]
    dest = tokens[-1]

    # Expand relative dest using workdir
    if dest == "." and workdir:
        dest = workdir.rstrip("/") + "/"

    is_parents = any("parents" in o and "false" not in o for o in options)
    dests: list[str] = []
    if dest.endswith("/"):
        for src in srcs:
            if check_parents and is_parents:
                new_dest = posixpath.join(dest, src.lstrip("./"))
            else:
                new_dest = posixpath.join(dest, posixpath.basename(src))
            dests.append(new_dest)
    else:
        dests.append(dest)
    return dests


def _analyze_run(value: str) -> list[dict]:
    """Parse RUN value into list of command dicts with keywords."""
    # Handle exec form
    stripped = value.strip()
    if stripped.startswith("["):
        try:
            tokens = json.loads(stripped)
            if tokens:
                return [{"command": tokens[0], "args": tokens[1:], "keywords": [], "dests": []}]
        except json.JSONDecodeError:
            pass

    # Strip --mount/--network flags
    remainder = stripped
    while remainder.startswith("--"):
        remainder = re.sub(r"^--\S+\s*", "", remainder)

    commands = _split_shell_commands(remainder)
    result: list[dict] = []
    for cmd_str in commands:
        cmd_str = cmd_str.strip()
        if not cmd_str or cmd_str.startswith("#"):
            continue
        try:
            tokens = shlex.split(cmd_str)
        except ValueError:
            tokens = cmd_str.split()
        if not tokens:
            continue

        # Skip pure variable assignments
        if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*=", tokens[0]):
            continue

        cmd: dict = {
            "command": tokens[0],
            "args": tokens[1:],
            "keywords": [],
            "dests": [],
        }

        # Recurse into bash -c "..."
        if cmd["command"] in ("bash", "sh") and len(cmd["args"]) >= 2 and cmd["args"][0] == "-c":
            sub = _analyze_run(cmd["args"][1])
            result.extend(sub)
            continue

        for arg in cmd["args"]:
            if not arg.startswith("-"):
                cmd["keywords"].append(arg)
        result.append(cmd)
    return result


def _split_shell_commands(s: str) -> list[str]:
    """Split on && ; respecting quotes."""
    parts: list[str] = []
    buf: list[str] = []
    in_single = in_double = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "'" and not in_double:
            in_single = not in_single
            buf.append(c)
        elif c == '"' and not in_single:
            in_double = not in_double
            buf.append(c)
        elif not in_single and not in_double:
            if c == "&" and i + 1 < len(s) and s[i + 1] == "&":
                parts.append("".join(buf))
                buf = []
                i += 2
                continue
            elif c == "|" and (i + 1 >= len(s) or s[i + 1] != "|"):
                parts.append("".join(buf))
                buf = []
            elif c == ";" :
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(c)
        else:
            buf.append(c)
        i += 1
    if buf:
        parts.append("".join(buf))
    return parts


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def _build_metadata_elems(meta_type: str, elem) -> list[MetadataElement]:
    result: list[MetadataElement] = []
    if meta_type in _DIC_TYPES:
        for k, v in elem.items():
            if meta_type == "labels":
                k = f'"{k}"'
                v = f'"{v}"'
            result.append(MetadataElement(meta_type, key=k, value=v))
    elif meta_type in _LIST_TYPES:
        for v in elem:
            result.append(MetadataElement(meta_type, value=str(v).replace("'", '"')))
    else:
        if elem is not None:
            v = elem.replace("'", '"') if isinstance(elem, str) else elem
            result.append(MetadataElement(meta_type, value=v))
    return result


# ---------------------------------------------------------------------------
# Main Enumerator (S1)
# ---------------------------------------------------------------------------

class Enumerator:
    def enumerate(
        self,
        dockerfile_path: str,
        image_name: str,
        progress=None,
    ) -> tuple[dict, dict]:
        def _p(msg: str) -> None:
            if progress:
                progress("S1", msg)

        _p("S1 — Parsing Dockerfile…")
        dfi = DockerfileInfo.from_path(dockerfile_path)

        _p("S1 — Running docker inspect…")
        insp = DockerInspectData(image_name)

        _p("S1 — Comparing Dockerfile vs image metadata…")
        metadata = self._set_metadata(dfi, insp)

        _p("S1 — Saving image tarball (may take a moment for large images)…")
        layers = self._get_layers(image_name, dfi)

        _p("S1 — Propagating whiteout deletions across layers…")
        layers = self._set_removed_path(layers)

        _p("S1 — Analysing Dockerfile instructions per layer…")
        layers = self._set_inst_info(layers)

        test_targets = {"metadata": metadata, "layers": layers}
        info = {
            "workdir": insp.workdir,
            "envs": insp.envs,
        }
        return test_targets, info

    def _set_metadata(self, dfi: DockerfileInfo, insp: DockerInspectData) -> list[MetadataElement]:
        dfile_raw = {
            "envVars": dfi.envs,
            "labels": dfi.labels,
            "exposedPorts": dfi.exposed_ports,
            "volumes": dfi.volumes,
            "entrypoint": dfi.entrypoint,
            "cmd": dfi.cmd,
            "workdir": dfi.workdir,
            "user": dfi.user,
        }
        ins_raw = {
            "envVars": insp.envs,
            "labels": insp.labels,
            "volumes": insp.volumes,
            "entrypoint": insp.entrypoint,
            "cmd": insp.cmd,
            "workdir": insp.workdir,
            "user": insp.user,
        }

        dfile_elems: list[MetadataElement] = []
        for t, v in dfile_raw.items():
            dfile_elems += _build_metadata_elems(t, v)
        for e in dfile_elems:
            e.from_dfile = True

        ins_elems: list[MetadataElement] = []
        for t, v in ins_raw.items():
            ins_elems += _build_metadata_elems(t, v)
        for e in ins_elems:
            e.from_ins = True

        # Mark inspect elements that are also in Dockerfile
        for ie in ins_elems:
            for de in list(dfile_elems):
                if ie.type == de.type:
                    if (ie.type in _DIC_TYPES and ie.equal_key(de)) or ie.equal_value(de):
                        ie.from_dfile = True
                        dfile_elems.remove(de)
                        break

        return ins_elems + dfile_elems

    def _get_layers(self, image_name: str, dfi: DockerfileInfo) -> list[Layer]:
        history, per_layer_data = _extract_layers_from_image(image_name)

        layers: list[Layer] = []
        layer_data_idx = 0

        for entry in history:
            is_empty = entry.get("empty_layer", False)
            created_by = entry.get("created_by", "")
            comment = entry.get("comment", "")
            if is_empty:
                layers.append(Layer(created_by, comment, [], []))
            else:
                if layer_data_idx < len(per_layer_data):
                    files, removed = per_layer_data[layer_data_idx]
                    layer_data_idx += 1
                else:
                    files, removed = [], []
                layers.append(Layer(created_by, comment, files, removed))

        # Correlate history entries with Dockerfile instructions (back-to-front)
        no_var = dfi.no_var_structure
        dfile_idx = len(no_var) - 1
        layer_idx = len(layers) - 1

        while layer_idx >= 0 and dfile_idx >= 0:
            insndesc = no_var[dfile_idx]
            layer = layers[layer_idx]
            instr = insndesc["instruction"]
            if instr == "FROM":
                layer.created_by = insndesc["content"]
                layer.workdir = dfi.get_now_workdir(insndesc["startline"])
                dfile_idx -= 1
                layer_idx -= 1
            elif (
                layer.created_by.startswith(instr)
                or layer.created_by.upper().startswith(instr)
                or _history_matches(layer.created_by, instr)
            ):
                layer.created_by = insndesc["content"]
                layer.workdir = dfi.get_now_workdir(insndesc["startline"])
                dfile_idx -= 1
                layer_idx -= 1
            else:
                dfile_idx -= 1

        return layers

    def _set_removed_path(self, layers: list[Layer]) -> list[Layer]:
        """Mark files deleted by a later layer as whiteouts.

        Walks the layers once, keeping the files seen so far, instead of
        re-scanning every preceding layer for every removed path — that was
        O(layers^2 x files) and dominated large images.
        """
        seen: list[File] = []          # every file from the layers before this one
        for layer in layers:
            extra: list[File] = []
            for removed_path in layer.removed_paths:
                prefix = removed_path + "/"
                for f in seen:
                    if f.path == removed_path or f.path.startswith(prefix):
                        extra.append(File(f.path, f.is_file, f.is_dir, "c---------"))
            seen.extend(layer.files)
            layer.files += extra
        return layers

    def _set_inst_info(self, layers: list[Layer]) -> list[Layer]:
        for layer in layers:
            inst_info = _analyze_instruction(layer.created_by, layer.workdir)
            layer.set_inst_info(inst_info)
        return layers


def _history_matches(created_by: str, instruction: str) -> bool:
    """Check if a Docker history entry corresponds to a Dockerfile instruction."""
    cb = created_by.upper()
    # BuildKit format: "INSTRUCTION value"
    if cb.startswith(instruction.upper()):
        return True
    # Classic format: "/bin/sh -c #(nop) INSTRUCTION value"
    nop = "#(NOP) " + instruction.upper()
    if nop in cb:
        return True
    # Classic RUN format: "/bin/sh -c command"
    if instruction.upper() == "RUN" and "/BIN/SH -C" in cb:
        return True
    return False
