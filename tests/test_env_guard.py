# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Test-environment guard: git itself refuses non-local transports.

The suite's real-git coverage uses local bare upstreams only, and the
hermetic `_git_env` gitconfig in conftest now sets
``protocol.allow = never`` with ``protocol.file.allow = user`` so any
accidental remote transport is refused by git, not just avoided by the
fixtures. These pins prove the guard discriminates: remote schemes
fail, local operations still work.
"""

import subprocess


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *map(str, args)], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(
            f"git {' '.join(map(str, args))} rc={r.returncode}:\n{r.stderr}")
    return r


def test_remote_transports_refused(tmp_path):
    """https/ssh/git:// transports are refused by git under the test env
    (conftest protocol.allow=never; requester-approved hardening)."""
    for url in (
        "https://example.invalid/x",
        "ssh://git@example.invalid/x",
        "git://example.invalid/x",
    ):
        r = _git("ls-remote", url, check=False)
        assert r.returncode != 0, f"{url} unexpectedly reachable"
        assert "not allowed" in r.stderr or "not allowed" in r.stdout


def test_local_path_and_file_scheme_still_work(tmp_path):
    """protocol.file.allow=user keeps local clones and file:// fetches
    working — the suite's real-git fixtures depend on them."""
    up = tmp_path / "up.git"
    _git("init", "--bare", str(up))
    # plain local-path clone (no transport machinery needed for it to
    # work, but it must not be refused either)
    _git("clone", str(up), str(tmp_path / "c1"))
    # explicit file:// clone exercises the allowed user file transport
    _git("clone", f"file://{up}", str(tmp_path / "c2"))
