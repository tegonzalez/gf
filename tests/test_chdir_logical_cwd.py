# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""`-C <path>` keeps the spelled logical cwd (cli `_apply_chdir`).

Pinned contract: a leading global `-C` operand is resolved against the
cwd that spelled it — `PWD` is set to the pre-chdir `abspath`, not an
abspath computed after `os.chdir` landed inside the destination. That
ordering is what lets `platform.logical_cwd` return the consumer
spelling (`<parent>/one`) instead of the physical `.gf/wt` checkout the
consumer link resolves to, and it is what feeds `select_children`'s
lexical tiebreak (`shelf.py` `_select_for_path`: the `named` arm that
keeps sibling links onto one shared checkout distinct under selection).

The defect this pins: computing the abspath AFTER `os.chdir(path)`
anchors a relative operand at the destination itself (`one` becomes
`<wt>/docs/api/one`), the same-inode check fails, `logical_cwd` falls
back to `os.getcwd()` — the physical store-checkout path — and a
spelled `-C` operand stops naming its own binding:

- `gf -C one pull` selected BOTH `one` and `two` (two bindings on the
  same upstream subdir share one checkout realpath, and with the
  lexical arm gone the realpath-inside set returned every innermost
  binding): `Pulled one` + `Pulled two` as two own-updates instead of
  `Pulled one` + `Pulled two (moved with one)`.
- `gf -C one sh -c 'echo $PWD'` exposed the physical `.gf/wt` checkout
  path (or the doubled `<wt>/docs/api/one`) instead of the spelled
  `.../parent/one`.

Fix under test (`cli.py`): `abspath(expanduser(path))` is computed
before `os.chdir` and `PWD` receives that pre-chdir abspath.

Real git over local bare upstreams via `gf clone`; selection is
observed through `gf pull` output lines, the spelled-cwd propagation
through `gf sh`'s environment.
"""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import gf, git


# ---------------------------------------------------------------------------
# helpers — setup failures report through pytest.fail, never assert/exceptions


def _git(*args, cwd: Path) -> None:
    try:
        git(*args, cwd=cwd)
    except subprocess.CalledProcessError as e:
        pytest.fail(f"setup git {' '.join(map(str, args))} failed: {e}")


def _gf_setup(*args, cwd: Path | None = None) -> None:
    r = gf(*map(str, args), cwd=cwd, check=False)
    if r.returncode != 0:
        pytest.fail(
            f"setup gf {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stdout}{r.stderr}")


def _pulled_lines(stdout: str) -> list[str]:
    """The `Pulled ...` lines of a pull's stdout, in order."""
    return [ln for ln in stdout.splitlines() if ln.startswith("Pulled")]


def _upstream(tmp_path: Path) -> Path:
    """Bare upstream carrying `docs/api/x.txt` on master."""
    up = tmp_path / "upstream"
    r = subprocess.run(
        ["git", "init", "--bare", str(up)],
        capture_output=True, text=True)
    if r.returncode != 0:
        pytest.fail(f"setup git init --bare failed:\n{r.stderr}")
    work = tmp_path / "_seed"
    r = subprocess.run(
        ["git", "clone", str(up), str(work)],
        capture_output=True, text=True)
    if r.returncode != 0:
        pytest.fail(f"setup git clone failed:\n{r.stderr}")
    (work / "docs/api").mkdir(parents=True)
    (work / "docs/api/x.txt").write_text("api on master")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "seed", cwd=work)
    _git("push", "-q", "origin", "HEAD:master", cwd=work)
    return up


def _two_bindings_one_subdir(tmp_path: Path) -> Path:
    """Parent repo with bindings `one` and `two` on the SAME upstream
    subdir — two consumer links onto one shared store checkout."""
    up = _upstream(tmp_path)
    parent = tmp_path / "parent"
    _git("init", str(parent), cwd=tmp_path)
    (parent / "README").write_text("root")
    _git("add", "README", cwd=parent)
    _git("commit", "-qm", "root", cwd=parent)

    _gf_setup("-C", parent, "clone", up / "docs/api", "one")
    _gf_setup("-C", parent, "clone", up / "docs/api", "two")

    one, two = parent / "one", parent / "two"
    if not (one.is_symlink() and two.is_symlink()):
        pytest.fail(f"setup did not leave consumer links: {one} {two}")
    if one.resolve() != two.resolve():
        pytest.fail("setup links do not share one checkout subdir")
    if f"{os.sep}.gf{os.sep}wt{os.sep}" not in str(one.resolve()):
        pytest.fail(f"setup link target is not a store checkout: "
                    f"{one.resolve()}")
    return parent


# ---------------------------------------------------------------------------
# pin 1 — selection: a spelled `-C` operand names only its own binding


def test_pull_dash_C_spelled_binding_selects_only_it(tmp_path):
    """`gf -C one pull` selects ONLY `one`: its own `Pulled one` line
    plus the co-moved sibling `Pulled two (moved with one)` — never a
    second own-update `Pulled two` (the pre-fix signature, where the
    physical fallback cwd selected every binding on the shared
    checkout)."""
    parent = _two_bindings_one_subdir(tmp_path)

    r = gf("-C", "one", "pull", cwd=parent, check=False)
    assert r.returncode == 0, r.stderr
    assert _pulled_lines(r.stdout) == [
        "Pulled one",
        "Pulled two (moved with one)",
    ]


def test_pull_cwd_inside_link_selects_only_its_binding(
        tmp_path, monkeypatch):
    """Equivalent entry point: `cd one && gf pull` (a real `cd` sets PWD
    to the consumer spelling) selects only `one`, same output as the
    `-C` arm — the fixed code path must not regress it."""
    parent = _two_bindings_one_subdir(tmp_path)

    monkeypatch.setenv("PWD", str(parent / "one"))
    r = gf("pull", cwd=parent / "one", check=False)
    assert r.returncode == 0, r.stderr
    assert _pulled_lines(r.stdout) == [
        "Pulled one",
        "Pulled two (moved with one)",
    ]


# ---------------------------------------------------------------------------
# pin 2 — propagation: the subprocess sees the LOGICAL spelling in $PWD


def test_dash_C_pwd_keeps_the_logical_spelling(tmp_path):
    """`gf -C one sh -c 'echo $PWD'` prints the spelled consumer path
    `parent/one` — never the physical `.gf/wt` checkout — and a symlink
    alias operand keeps its own spelling too."""
    parent = _two_bindings_one_subdir(tmp_path)

    r = gf("-C", "one", "sh", "-c", "echo $PWD", cwd=parent, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(parent / "one")
    assert ".gf" not in r.stdout

    alias = parent / "alias"
    os.symlink("one", alias)
    r = gf("-C", "alias", "sh", "-c", "echo $PWD", cwd=parent,
           check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(alias)


# ---------------------------------------------------------------------------
# pin 3 — the operand error path still names what it was given


def test_dash_C_nonexistent_operand_dies_naming_it(tmp_path):
    """`gf -C nonexistent pull` dies cleanly: `cannot change to
    'nonexistent'` on stderr, non-zero exit, no traceback."""
    parent = _two_bindings_one_subdir(tmp_path)

    r = gf("-C", "nonexistent", "pull", cwd=parent, check=False)
    assert r.returncode == 1
    assert "cannot change to 'nonexistent'" in r.stderr
    assert "Traceback" not in r.stderr


# ---------------------------------------------------------------------------
# pin 4 — control: `-C .` at the parent root still selects every binding


def test_dash_C_dot_pull_selects_all_bindings(tmp_path):
    """`gf -C . pull` from the parent root is the unscoped selection:
    both bindings get their own `Pulled` line and nothing is reported
    `(moved with ...)`."""
    parent = _two_bindings_one_subdir(tmp_path)

    r = gf("-C", ".", "pull", cwd=parent, check=False)
    assert r.returncode == 0, r.stderr
    assert _pulled_lines(r.stdout) == ["Pulled one", "Pulled two"]
