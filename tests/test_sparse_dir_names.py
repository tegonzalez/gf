# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""F4/F5 (round-2) + R5-E (round-5): bound subdir names that collide
with the sparse-checkout CLI surface.

`gf` feeds the cone union to `git sparse-checkout set --cone` as
DIRECTORY OPERANDS. A bound subdir whose name is also meaningful to
that command — a gitignore glob character class (`app/[id]`), an
option spelling (top-level `--no-cone`), or a `!`-leading name — must
reach git literally:

- `app/[id]`: without `--skip-checks` git rejects a dir operand
  containing `*?[]\\` ("specify directories rather than patterns"),
  and a raw `/app/[id]/` pattern line would be a character class that
  never matches the literal directory. The link must materialize
  `page.txt` (verified on git 2.47.3: `set --cone --skip-checks` writes
  `/app/\\[id]/` and materializes it). The pin is conditional on the
  installed git supporting `--skip-checks` (git >= 2.36 era).
- `--no-cone`: without a `--` separator the operand is eaten as the
  flag that disables cone mode — the store ends up non-cone and the
  link dangles. The link must be non-dangling with `n.txt`
  materialized and cone mode still on.
- `!foo`: an operand-leading `!` hits the same sanitize check —
  git dies "specify directories rather than patterns. If your
  directory starts with a '!', pass --skip-checks" unless the union is
  applied with `--skip-checks` (verified on git 2.47.3; GF-TRB-9).
  A `!`-leading `/`-segment mid-path (`app/!foo`, the register's
  example) is accepted natively — flagging it too is a harmless
  superset, since the flag only relaxes operand validation.

Authorities: spec Subfolder-binding layout / clone mechanics
("sparse-checked-out in cone mode"); round-2 architecture rulings
F4/F5; round-5 finding R5-E retiring known-issue GF-TRB-9
(docs/gf-troubleshooting.md).
"""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import gf, git
from gf import layout


# --- fixtures ----------------------------------------------------------


def _parent(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    parent.mkdir()
    git("init", cwd=parent)
    (parent / "README").write_text("root")
    git("add", "README", cwd=parent)
    git("commit", "-m", "root", cwd=parent)
    return parent


def _upstream(tmp_path: Path) -> Path:
    """Bare upstream, master tree: `app/[id]/page.txt` (glob-char dir)
    and top-level `--no-cone/n.txt` (option-spelled dir)."""
    up = tmp_path / "upstream"
    up.mkdir()
    git("init", "--bare", cwd=up)
    work = tmp_path / "_seed"
    git("clone", str(up), str(work), cwd=tmp_path)
    (work / "app" / "[id]").mkdir(parents=True)
    (work / "app" / "[id]" / "page.txt").write_text("page\n")
    (work / "--no-cone").mkdir()
    (work / "--no-cone" / "n.txt").write_text("n\n")
    git("add", "-A", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("push", "-q", "origin", "master", cwd=work)
    return up


def _cone_skip_checks_supported(tmp_path: Path) -> bool:
    """Whether this git takes `sparse-checkout set --cone --skip-checks
    <dir>` — probed for real on a scratch repo (git 2.47.3 does; the
    spec floor does not)."""
    repo = tmp_path / "_cap"
    git("init", str(repo), cwd=tmp_path)
    r = subprocess.run(
        ["git", "-C", str(repo), "sparse-checkout", "set", "--cone",
         "--skip-checks", "x"],
        capture_output=True, text=True)
    return r.returncode == 0


def _require_skip_checks(tmp_path: Path) -> None:
    if not _cone_skip_checks_supported(tmp_path):
        pytest.skip(
            "git `sparse-checkout set --cone --skip-checks` unsupported "
            "on this git")


def _rk(parent: Path) -> Path:
    wt = parent / ".gf" / "wt"
    entries = [p for p in wt.iterdir() if p.is_dir()]
    assert len(entries) == 1, f"expected one repo store, found {entries}"
    return entries[0]


def _admin(parent: Path, up: Path, key: str = "master") -> Path:
    """The `<store>/worktrees/<key>` admin dir for `up`'s checkout."""
    return layout.repo_store(parent, str(up)) / "worktrees" / key


def _cone_enabled(parent: Path, up: Path, key: str = "master") -> str:
    return subprocess.run(
        ["git", "--git-dir", str(_admin(parent, up, key)),
         "config", "--get", "core.sparseCheckoutCone"],
        capture_output=True, text=True).stdout.strip()


def _sparse_file(parent: Path, up: Path, key: str = "master") -> str:
    f = _admin(parent, up, key) / "info" / "sparse-checkout"
    return f.read_text() if f.is_file() else ""


# --- F4: glob-char dir name ---------------------------------------------


def test_clone_subdir_with_glob_chars_materializes(tmp_path):
    """F4: `gf clone <url>/app/[id] vendor/appid` — `[id]` is a
    gitignore character class, so the cone union must reach git as a
    directory operand (`--skip-checks`), not a raw pattern line that
    can never match the literal name. The consumer link is
    non-dangling and materializes `page.txt`."""
    _require_skip_checks(tmp_path)
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)

    r = gf("-C", str(parent), "clone", str(up / "app" / "[id]"),
           "vendor/appid", check=False)
    assert r.returncode == 0, r.stderr

    link = parent / "vendor" / "appid"
    assert link.is_symlink()
    assert link.exists(), "dangling consumer link"
    assert (link / "page.txt").read_text() == "page\n"

    # the hidden checkout materialized the literal dir
    rk = _rk(parent)
    assert (rk / "master" / "app" / "[id]" / "page.txt").is_file()


# --- F5: option-spelled dir name ----------------------------------------


def test_clone_subdir_named_like_sparse_flag_stays_cone(tmp_path):
    """F5: `gf clone <url>/--no-cone vendor/dash` — the union's dir list
    must be `--`-separated, or `--no-cone` is parsed as the flag that
    turns cone mode OFF. The link is non-dangling, `n.txt`
    materializes, and the checkout's cone mode stays on."""
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)

    r = gf("-C", str(parent), "clone", str(up / "--no-cone"),
           "vendor/dash", check=False)
    assert r.returncode == 0, r.stderr

    link = parent / "vendor" / "dash"
    assert link.is_symlink()
    assert link.exists(), "dangling consumer link"
    assert (link / "n.txt").read_text() == "n\n"

    # cone mode still on: a cone-style `/--no-cone/` dir pattern, and
    # the worktree config says so
    assert "/--no-cone/" in _sparse_file(parent, up).splitlines()
    assert _cone_enabled(parent, up) == "true"


def test_clone_widening_keeps_special_dir_names(tmp_path):
    """F4+F5 union: the second clone rewrites the cone union — the
    `--`-separation and `--skip-checks` admission must survive the
    rebuild that mixes both special names. Both links materialize;
    cone stays on."""
    _require_skip_checks(tmp_path)
    up = _upstream(tmp_path)
    parent = _parent(tmp_path)

    gf("-C", str(parent), "clone", str(up / "app" / "[id]"),
       "vendor/appid")
    r = gf("-C", str(parent), "clone", str(up / "--no-cone"),
           "vendor/dash", check=False)
    assert r.returncode == 0, r.stderr

    appid = parent / "vendor" / "appid"
    dash = parent / "vendor" / "dash"
    assert appid.is_symlink() and appid.exists()
    assert dash.is_symlink() and dash.exists()
    assert (appid / "page.txt").read_text() == "page\n"
    assert (dash / "n.txt").read_text() == "n\n"

    sparse = _sparse_file(parent, up)
    assert "/--no-cone/" in sparse.splitlines()
    assert "[id]" in sparse   # the escaped literal reaches the file
    assert _cone_enabled(parent, up) == "true"


# --- R5-E: `!`-leading dir names (retires GF-TRB-9) -----------------------


def _bang_upstream(tmp_path: Path) -> Path:
    """Bare upstream, master tree: `!foo/bang.txt` (operand-leading `!`),
    `app/!foo/nested.txt` and `app/!bang/bang.txt` (`!`-leading
    `/`-segments — the GF-TRB-9 register's named spelling)."""
    up = tmp_path / "upstream"
    up.mkdir()
    git("init", "--bare", cwd=up)
    work = tmp_path / "_seed"
    git("clone", str(up), str(work), cwd=tmp_path)
    (work / "!foo").mkdir()
    (work / "!foo" / "bang.txt").write_text("bang\n")
    (work / "app" / "!foo").mkdir(parents=True)
    (work / "app" / "!foo" / "nested.txt").write_text("nested\n")
    (work / "app" / "!bang").mkdir(parents=True)
    (work / "app" / "!bang" / "bang.txt").write_text("midbang\n")
    git("add", "-A", cwd=work)
    git("commit", "-m", "init", cwd=work)
    git("push", "-q", "origin", "master", cwd=work)
    return up


def test_clone_subdir_with_leading_bang_materializes(tmp_path):
    """R5-E (GF-TRB-9): `gf clone <url>/!foo vendor/bang` — a root-level
    `!` makes the cone operand itself start with `!`, which
    `sparse-checkout set --cone` rejects as a pattern ("specify
    directories rather than patterns. If your directory starts with a
    '!', pass --skip-checks" — verified on git 2.47.3). The union must
    reach git under `--skip-checks`; the link materializes `bang.txt`
    and the hidden checkout carries the literal `!foo` tree."""
    _require_skip_checks(tmp_path)
    up = _bang_upstream(tmp_path)
    parent = _parent(tmp_path)

    r = gf("-C", str(parent), "clone", str(up / "!foo"),
           "vendor/bang", check=False)
    assert r.returncode == 0, r.stderr

    link = parent / "vendor" / "bang"
    assert link.is_symlink()
    assert link.exists(), "dangling consumer link"
    assert (link / "bang.txt").read_text() == "bang\n"

    # the hidden checkout materialized the literal `!`-leading dir
    rk = _rk(parent)
    assert (rk / "master" / "!foo" / "bang.txt").is_file()
    assert "/!foo/" in _sparse_file(parent, up).splitlines()


def test_clone_widening_with_bang_members_keeps_both(tmp_path):
    """R5-E union rewrite: a second `!`-member binding joining the same
    checkout rebuilds the cone union to `!foo` + `app/!bang` — the
    `--skip-checks` admission must survive that rebuild just as it does
    the first admission. Both consumer links materialize; cone mode
    stays on."""
    _require_skip_checks(tmp_path)
    up = _bang_upstream(tmp_path)
    parent = _parent(tmp_path)

    gf("-C", str(parent), "clone", str(up / "!foo"), "vendor/bang")
    r = gf("-C", str(parent), "clone", str(up / "app" / "!bang"),
           "vendor/midbang", check=False)
    assert r.returncode == 0, r.stderr

    bang = parent / "vendor" / "bang"
    mid = parent / "vendor" / "midbang"
    assert bang.is_symlink() and bang.exists()
    assert mid.is_symlink() and mid.exists()
    assert (bang / "bang.txt").read_text() == "bang\n"
    assert (mid / "bang.txt").read_text() == "midbang\n"

    lines = _sparse_file(parent, up).splitlines()
    assert "/!foo/" in lines
    assert "/app/!bang/" in lines
    assert _cone_enabled(parent, up) == "true"


def test_clone_subdir_with_mid_path_bang_segment(tmp_path):
    """R5-E / the GF-TRB-9 register's named example: `gf clone
    <url>/app/!foo vendor/nested` — a `!`-leading `/`-segment rather
    than an operand-leading one. git's operand check fires only on a
    leading `!` (2.47.3 accepts `app/!foo` with or without the flag);
    gf admits it through the same `--skip-checks` union — a harmless
    superset — and the binding materializes `nested.txt`."""
    up = _bang_upstream(tmp_path)
    parent = _parent(tmp_path)

    r = gf("-C", str(parent), "clone", str(up / "app" / "!foo"),
           "vendor/nested", check=False)
    assert r.returncode == 0, r.stderr

    link = parent / "vendor" / "nested"
    assert link.is_symlink()
    assert link.exists(), "dangling consumer link"
    assert (link / "nested.txt").read_text() == "nested\n"
    assert "/app/!foo/" in _sparse_file(parent, up).splitlines()
    assert _cone_enabled(parent, up) == "true"


def test_sparse_union_flags_bang_operand_for_skip_checks(
    fs, gf_inproc, mock_backend
):
    """R5-E version-independent pin: applying the cone union for a
    `!`-leading member must reach the backend as exactly
    `sparse-checkout set --cone --skip-checks -- !foo` — the flag on
    the `--`-separated operand list, so git never pattern-checks the
    operand. Pinned on the backend call log, independent of the
    installed git's sanitize set."""
    parent = Path("/parent")
    (parent / ".git").mkdir(parents=True)
    (parent / "gf.toml").write_text("git_folder = []\n")

    # `/upstream` needs bare-repo markers on disk so the local URL
    # walk-up resolves `!foo` as its subdir, plus a seeded commit tree.
    up = Path("/upstream")
    (up / "objects").mkdir(parents=True)
    (up / "refs").mkdir()
    (up / "HEAD").write_text("ref: refs/heads/master\n")
    repo = mock_backend.seed(up, bare=True)
    sha = "3" * 40
    mock_backend.add_commit(repo, sha, {"!foo/bang.txt": "bang\n"})
    repo.refs["refs/heads/master"] = sha

    r = gf_inproc("-C", str(parent), "clone", "/upstream/!foo",
                  "vendor/bang", backend=mock_backend, check=False)
    assert r.returncode == 0, r.stderr

    sparse_calls = [
        c[0] for c in mock_backend.calls if c[0][0] == "sparse-checkout"
    ]
    assert sparse_calls == [
        ("sparse-checkout", "set", "--cone", "--skip-checks", "--",
         "!foo")
    ]

    # the binding is served: the link resolves into the shared checkout
    # and the mapped file is readable through it
    link = parent / "vendor" / "bang"
    assert link.is_symlink()
    assert (link / "bang.txt").read_text() == "bang\n"
