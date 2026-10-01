"""Regression tests for test_generation.

This module had no tests, which is why several of these bugs survived so long.
Everything here is pure — no Docker.
"""

import pytest

from test_generation.builder import _split_run_value, split_run_commands
from test_generation.data_structures import File, Layer, MetadataElement
from test_generation.enumerator import _normalize_path
from test_generation.viewpoint import ViewpointDeterminer

# ---------------------------------------------------------------------------
# builder — RUN options were emitted twice
# ---------------------------------------------------------------------------

def test_mount_option_is_not_duplicated():
    out = _split_run_value("--mount=type=cache,target=/root/.cache pip install x")
    assert len(out) == 1
    assert out[0].count("--mount=type=cache,target=/root/.cache") == 1
    assert out[0] == "RUN --mount=type=cache,target=/root/.cache pip install x"


def test_multiple_options_are_not_duplicated():
    out = _split_run_value("--network=none --security=insecure make build")
    assert out[0].count("--network=none") == 1
    assert out[0].count("--security=insecure") == 1


def test_option_preserved_when_the_command_is_split():
    out = _split_run_value("--mount=type=cache apt-get update && apt-get install -y curl")
    assert len(out) == 2
    for line in out:
        assert line.count("--mount=type=cache") == 1


def test_plain_run_is_untouched():
    assert _split_run_value("echo hello") == ["RUN echo hello"]


def test_compound_run_splits_into_layers():
    out = _split_run_value("apt-get update && apt-get install -y curl")
    assert out == ["RUN apt-get update", "RUN apt-get install -y curl"]


def test_cd_is_carried_forward():
    out = _split_run_value("cd /src && make && make install")
    assert out[0] == "RUN cd /src"
    assert all("cd /src" in line for line in out[1:])


def test_split_run_commands_leaves_other_instructions_alone():
    dockerfile = "FROM alpine\nRUN a && b\nCMD [\"x\"]\n"
    out = split_run_commands(dockerfile)
    assert "FROM alpine" in out
    assert 'CMD ["x"]' in out
    assert out.count("RUN ") == 2


# ---------------------------------------------------------------------------
# enumerator — lstrip('.') ate the dot of a dotfile
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("member,expected", [
    ("./.bashrc", "/.bashrc"),          # the regression
    ("./.config/app.ini", "/.config/app.ini"),
    ("./usr/bin/node", "/usr/bin/node"),
    ("usr/bin/node", "/usr/bin/node"),
    ("./", "/"),
    (".", "/"),
    ("./app/.env", "/app/.env"),
])
def test_tar_member_paths_normalise(member, expected):
    assert _normalize_path(member) == expected


def test_leading_dots_are_not_stripped_repeatedly():
    assert _normalize_path("./..hidden") == "/..hidden"


# ---------------------------------------------------------------------------
# data_structures — non-string values raised AttributeError
# ---------------------------------------------------------------------------

def test_integer_metadata_values_compare():
    a = MetadataElement("exposedPorts", value=8081)
    b = MetadataElement("exposedPorts", value="8081")
    assert a.equal_value(b) is True          # used to raise AttributeError


def test_none_metadata_value_compares():
    a = MetadataElement("labels", key="x", value=None)
    b = MetadataElement("labels", key="x", value="None")
    assert a.equal_value(b) is True


def test_list_metadata_values_still_compare():
    a = MetadataElement("cmd", value=["node", "index.js"])
    b = MetadataElement("cmd", value=["node", "index.js"])
    assert a.equal_value(b) is True


def test_non_string_keys_compare():
    a = MetadataElement("labels", key=8081, value="x")
    b = MetadataElement("labels", key="8081", value="x")
    assert a.equal_key(b) is True


# ---------------------------------------------------------------------------
# viewpoint — which files are even worth probing
# ---------------------------------------------------------------------------

def _vd():
    return ViewpointDeterminer("img", ["/usr/local/bin", "/usr/bin", "/bin"])


def test_candidates_keeps_files_on_path():
    files = [File("/usr/bin/node", True, False, "-rwxr-xr-x")]
    assert _vd()._candidates(files) == {"node": "/usr/bin/node"}


def test_candidates_skips_directories_whiteouts_and_off_path():
    files = [
        File("/usr/bin/somedir", False, True, "drwxr-xr-x"),
        File("/usr/bin/gone", True, False, "c---------"),
        File("/opt/elsewhere/tool", True, False, "-rwxr-xr-x"),
    ]
    assert _vd()._candidates(files) == {}


def test_candidates_drops_ambiguous_names():
    """One lookup cannot answer for two paths sharing a basename."""
    files = [
        File("/usr/bin/python", True, False, "-rwxr-xr-x"),
        File("/usr/local/bin/python", True, False, "-rwxr-xr-x"),
    ]
    assert "python" not in _vd()._candidates(files)


def test_candidates_keeps_a_repeated_identical_path():
    f = File("/usr/bin/node", True, False, "-rwxr-xr-x")
    assert _vd()._candidates([f, f]) == {"node": "/usr/bin/node"}


def test_empty_candidate_set_needs_no_container():
    """No probe should be attempted when nothing qualifies."""
    assert _vd()._resolve_batch([], container_id="nonexistent") == {}


# ---------------------------------------------------------------------------
# enumerator — whiteout propagation, now single-pass
# ---------------------------------------------------------------------------

def test_removed_paths_mark_earlier_files_as_whiteouts():
    from test_generation.enumerator import Enumerator

    l1 = Layer("RUN a", "", [File("/app/x.txt", True, False, "-rw-r--r--"),
                             File("/app/keep.txt", True, False, "-rw-r--r--")], [])
    l2 = Layer("RUN b", "", [], ["/app/x.txt"])
    out = Enumerator()._set_removed_path([l1, l2])

    whiteouts = [f.path for l in out for f in l.files if f.permissions.startswith("c")]
    assert whiteouts == ["/app/x.txt"]
    assert "/app/keep.txt" not in whiteouts


def test_removing_a_directory_marks_everything_under_it():
    from test_generation.enumerator import Enumerator

    l1 = Layer("RUN a", "", [File("/opt/d/one", True, False, "-rw-r--r--"),
                             File("/opt/d/two", True, False, "-rw-r--r--"),
                             File("/opt/dother", True, False, "-rw-r--r--")], [])
    l2 = Layer("RUN b", "", [], ["/opt/d"])
    out = Enumerator()._set_removed_path([l1, l2])

    whiteouts = {f.path for l in out for f in l.files if f.permissions.startswith("c")}
    assert whiteouts == {"/opt/d/one", "/opt/d/two"}
    assert "/opt/dother" not in whiteouts     # prefix must not match a sibling


def test_a_later_layers_own_files_are_not_whiteouts():
    from test_generation.enumerator import Enumerator

    l1 = Layer("RUN a", "", [], [])
    l2 = Layer("RUN b", "", [File("/app/new.txt", True, False, "-rw-r--r--")],
               ["/app/new.txt"])
    out = Enumerator()._set_removed_path([l1, l2])
    assert not any(f.permissions.startswith("c") for l in out for f in l.files)
