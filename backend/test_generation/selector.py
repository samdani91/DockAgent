"""S2: Score and filter test targets using the 20 scoring rules from the paper."""

import fnmatch
import posixpath
import re
from typing import Callable

from cancellation import check_cancelled

from .data_structures import File, Layer, MetadataElement

# -- Rule 21 (extension, not from the paper) --
# Documentation and packaging metadata otherwise outrank real application files:
# an npm man page picks up keyword points from its RUN instruction while a
# plain source file does not. Kept deliberately general — no package-specific
# paths.
_DOC_DIR_MARKERS = ("/man/", "/doc/", "/docs/", "/.git/")
_DOC_SUFFIXES = (".md", ".rst", ".markdown")
_DOC_BASENAMES = ("license", "licence", "copying", "notice", "authors", "changelog")
# Man-page section suffixes (foo.1, foo.3.gz). Only applied when the path also
# mentions "man", so shared libraries like libc.so.6 are not caught.
_MAN_SECTION_RE = re.compile(r"\.[1-9](\.gz)?$")


def _is_documentation(path: str) -> bool:
    lowered = path.lower()
    if any(marker in lowered for marker in _DOC_DIR_MARKERS):
        return True

    base = posixpath.basename(lowered)
    if base.endswith(_DOC_SUFFIXES):
        return True
    if any(base.startswith(name) for name in _DOC_BASENAMES):
        return True
    if "man" in lowered and _MAN_SECTION_RE.search(base):
        return True
    return False


class Scorer:
    def score(
        self, test_targets: dict, info: dict,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict:
        self._cancelled = cancelled
        self._info = info
        path_env = info["envs"].get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
        self._command_search_paths = path_env.split(":")
        self._env_paths = [v for k, v in info["envs"].items() if k != "PATH" and v]

        scored_metadata = self._score_metadata(test_targets["metadata"])
        scored_layers = self._score_files(test_targets["layers"])

        return {"metadata": scored_metadata, "layers": scored_layers}

    # -- Rule 1 & 2: metadata --

    def _score_metadata(self, metadata: list[MetadataElement]) -> list[MetadataElement]:
        for elem in metadata:
            if elem.from_dfile:
                elem.points += 10   # Rule 1
            if elem.from_ins:
                elem.points += 8    # Rule 2
        return metadata

    # -- Rules 3–20: files --

    def _score_files(self, layers: list[Layer]) -> list[Layer]:
        for layer in layers:
            check_cancelled(self._cancelled)
            inst = layer.inst_info or {}
            for index, f in enumerate(layer.files):
                if index % 256 == 0:
                    check_cancelled(self._cancelled)
                self._apply_inst_rules(f, inst)
                self._apply_path_rules(f)

        # De-duplicate: accumulate inst_points for same path seen in multiple layers
        seen: dict[str, File] = {}
        for layer in reversed(layers):
            check_cancelled(self._cancelled)
            # Identity set, not a list: the old `x not in to_remove` was a
            # linear scan per file, so de-duplication was quadratic.
            to_remove: set[int] = set()
            for index, f in enumerate(layer.files):
                if index % 256 == 0:
                    check_cancelled(self._cancelled)
                if f.path in seen:
                    seen[f.path].inst_points += f.inst_points
                    to_remove.add(id(f))
                else:
                    seen[f.path] = f
            layer.files = [x for x in layer.files if id(x) not in to_remove]

        return layers

    def _apply_inst_rules(self, f: File, inst: dict) -> None:
        # Rule 3 & 4: ADD/COPY destinations
        for key in ("add", "copy"):
            if inst.get(key):
                for dest in inst.get("dests", []):
                    if self._custom_fnmatch(f.path, dest, end=True):
                        f.inst_points += 9   # Rule 3: exact dest match
                    elif self._custom_fnmatch(f.path, dest, end=False):
                        f.inst_points += 3   # Rule 4: prefix dest match

        # Rule 5 & 6: RUN command keywords in file path
        if inst.get("run"):
            seen_keywords: list[str] = []
            for cmd in inst.get("run_commands", []):
                for kw in cmd.get("keywords", []):
                    if kw not in seen_keywords and kw in f.path:
                        seen_keywords.append(kw)
                        if self._custom_fnmatch(f.path, kw, end=True):
                            f.inst_points += 5   # Rule 5
                        else:
                            f.inst_points += 2   # Rule 6

        # Rule 7: FROM base-image keyword in file path
        if inst.get("from"):
            for kw in inst.get("from_keywords", []):
                if kw in f.path:
                    f.inst_points += 3   # Rule 7

        # Rule 8: FROM layer penalty
        if inst.get("from"):
            f.inst_points -= 5   # Rule 8

    def _apply_path_rules(self, f: File) -> None:
        # Rule 9: whiteout
        if f.permissions.startswith("c"):
            f.path_points -= 10   # Rule 9

        # Rule 10: file path == workdir
        if f.path == self._info.get("workdir"):
            f.path_points += 3   # Rule 10

        # Rule 11: file path under non-PATH env value
        for ep in self._env_paths:
            if f.path.startswith(ep):
                f.path_points += 2   # Rule 11

        # Rule 12: file path under PATH component
        for sp in self._command_search_paths:
            if f.path.startswith(sp):
                f.path_points += 2   # Rule 12

        # Rule 13: /bin/ in path
        if "/bin/" in f.path:
            f.path_points += 3   # Rule 13

        # Rule 14: /etc/ in path
        if "/etc/" in f.path:
            f.path_points += 3   # Rule 14

        # Rule 15: /conf/ in path
        if "/conf/" in f.path:
            f.path_points += 3   # Rule 15

        # Rule 16: .sh file
        if f.path.endswith(".sh"):
            f.path_points += 3   # Rule 16

        # Rule 17: apt lists (penalty)
        if f.path.startswith("/var/lib/apt/lists/"):
            f.path_points -= 10   # Rule 17

        # Rule 18: /tmp/ (penalty)
        if "/tmp/" in f.path:
            f.path_points -= 10   # Rule 18

        # Rule 19: /cache/ (penalty)
        if "/cache/" in f.path:
            f.path_points -= 10   # Rule 19

        # Rule 20: /log/ (penalty)
        if "/log/" in f.path:
            f.path_points -= 10   # Rule 20

        # Rule 21 (extension): documentation / packaging metadata (penalty)
        if _is_documentation(f.path):
            f.path_points -= 10   # Rule 21

    # -- fnmatch helper (exact port from paper reference) --

    def _custom_fnmatch(self, filename: str, pattern: str, end: bool) -> bool:
        pattern = pattern.replace("~/", "")
        pattern = repr(pattern)[1:-1]
        pattern = pattern.replace("[^", "[!")
        pattern = self._replace_backslash_c(pattern)
        regex = fnmatch.translate(pattern)
        regex = regex.replace(r".*", r"[^/]*")
        regex = r".*" + regex
        if not end:
            regex = regex.replace(r"\Z", r"/.*\Z")
        return re.match(regex, filename) is not None

    @staticmethod
    def _replace_backslash_c(pattern: str) -> str:
        result = ""
        i = 0
        while i < len(pattern):
            if pattern[i: i + 2] == r"\\" and i + 2 < len(pattern):
                result += f"[{pattern[i + 2]}]"
                i += 3
            else:
                result += pattern[i]
                i += 1
        return result


class Filter:
    def filter(
        self, test_targets: dict, threshold: float,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict:
        check_cancelled(cancelled)
        metadata = [e for e in test_targets["metadata"] if e.points >= threshold]
        layers = self._filter_files(test_targets["layers"], threshold, cancelled)
        return {"metadata": metadata, "layers": layers}

    def _filter_files(
        self, layers: list[Layer], threshold: float,
        cancelled: Callable[[], bool] | None = None,
    ) -> list[Layer]:
        for layer in layers:
            check_cancelled(cancelled)
            layer.files = [f for f in layer.files if f.points >= threshold]
        return layers
