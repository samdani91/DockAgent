"""Tests for test_generation/selector.py scoring rules.

Note: this is a different module from `test_select.py`, which covers patch
selection in `dockerfile_generation`. Same word, unrelated code.
"""

import pytest

from test_generation.data_structures import File, Layer
from test_generation.selector import Filter, Scorer, _is_documentation

_DEFAULT_INFO = {
    "envs": {"PATH": "/usr/local/bin:/usr/bin:/bin"},
    "workdir": "/usr/src/app",
}

# An instruction that installs npm — this is what gives man pages their
# keyword points and lets them outrank application files.
_NPM_INST = {
    "run": True,
    "run_commands": [{"keywords": ["npm"]}],
}


def _score(path: str, inst: dict | None = None, permissions: str = "-rw-r--r--") -> int:
    f = File(path=path, is_file=True, is_dir=False, permissions=permissions)
    layer = Layer(created_by="RUN npm install -g npm", comment="", files=[f],
                  removed_paths=[])
    layer.set_inst_info(inst if inst is not None else _NPM_INST)
    Scorer().score({"metadata": [], "layers": [layer]}, dict(_DEFAULT_INFO))
    return f.points


# ---------------------------------------------------------------------------
# Rule 21 — documentation penalty
# ---------------------------------------------------------------------------

def test_man_page_scores_below_application_source():
    man_page = _score("/usr/local/lib/node_modules/npm/man/man1/npm-install.1")
    app_file = _score("/usr/src/app/index.js")
    assert man_page < app_file, (
        f"man page scored {man_page}, application file scored {app_file}"
    )


def test_gzipped_man_page_is_penalised():
    assert _score("/usr/share/man/man3/printf.3.gz") < _score("/usr/src/app/server.js")


@pytest.mark.parametrize("path", [
    "/usr/share/doc/npm/README.md",
    "/app/docs/guide.rst",
    "/srv/repo/.git/config",
    "/usr/local/lib/node_modules/npm/LICENSE",
    "/app/CHANGELOG.md",
])
def test_documentation_paths_are_detected(path):
    assert _is_documentation(path) is True


@pytest.mark.parametrize("path", [
    "/usr/src/app/index.js",
    "/usr/local/bin/node",
    "/etc/nginx/nginx.conf",
    "/usr/lib/x86_64-linux-gnu/libc.so.6",   # NOT a man page
    "/app/entrypoint.sh",
    "/usr/lib/libfoo.so.1",                  # NOT a man page
])
def test_real_files_are_not_flagged_as_documentation(path):
    assert _is_documentation(path) is False


def test_shared_library_outranks_man_page():
    """Guards the .so.N false positive the section regex could introduce."""
    assert _score("/usr/lib/libssl.so.1") > _score("/usr/share/man/man1/openssl.1")


# ---------------------------------------------------------------------------
# Existing rules still hold
# ---------------------------------------------------------------------------

def test_whiteout_file_is_penalised():
    assert _score("/app/removed.txt", permissions="c---------") < 0


def test_bin_path_scores_above_plain_path():
    assert _score("/usr/local/bin/node") > _score("/opt/random/node")


def test_filter_drops_below_threshold():
    low = File(path="/usr/share/man/man1/x.1", is_file=True, is_dir=False,
               permissions="-rw-r--r--")
    high = File(path="/usr/local/bin/node", is_file=True, is_dir=False,
                permissions="-rwxr-xr-x")
    layer = Layer(created_by="RUN npm i", comment="", files=[low, high],
                  removed_paths=[])
    layer.set_inst_info(_NPM_INST)

    scored = Scorer().score({"metadata": [], "layers": [layer]}, dict(_DEFAULT_INFO))
    filtered = Filter().filter(scored, threshold=1)

    kept = [f.path for l in filtered["layers"] for f in l.files]
    assert "/usr/local/bin/node" in kept
    assert "/usr/share/man/man1/x.1" not in kept
