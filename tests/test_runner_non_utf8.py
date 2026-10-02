# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

"""Non-UTF-8 subprocess output → capture-mode decode pins.

`run_command(mode="capture")` pipes the child through
`subprocess.run(text=True)`; without `errors="replace"` the pipes
decode under strict UTF-8 and a single non-UTF-8 byte in git's reply —
a refname like `refs/heads/bad\\xffb` served by a real upstream —
raises UnicodeDecodeError out of the runner and escapes `cli.main`
(which envelopes only GitFoldersError/OSError) as a raw `Traceback`.
Stream mode never had the bug: it shovels bytes and decodes its
collected chunks with `errors="replace"` (~runner.py:125). The fix
under test adds the same `errors="replace"` to the capture call
(runner.py:88), so a 0xff byte lands as U+FFFD in the captured string
and the clone dies inside the `gf:` envelope instead of tracebacking.

Pre-fix signature (verified by reverting the worktree fix —
`git diff > /tmp/x.diff; git checkout HEAD -- src/gf/runner.py`,
running this file, then `git apply /tmp/x.diff`): every discriminating
arm raises UnicodeDecodeError — the unit arm inside subprocess.run's
pipe decode, the integration arm inside clone's `git ls-remote`
capture during remote-URL resolution — while the ASCII/exit-code
controls pass on both sides.

The integration arm needs a filesystem that preserves a raw 0xff byte
in filenames: loose refs and the remote-tracking name are files. It
probes the conftest scratch dir first and falls back to pytest's
basetemp, skipping where every candidate sanitizes (e.g. virtiofs
silently rewrites invalid-UTF-8 names to their U+FFFD encoding — the
conftest tmp root lives on such a mount on some dev hosts).
"""

import codecs
import locale
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gf import runner


# The upstream's default branch carries one literal 0xff byte. A str
# argv can never spell it — os.fsencode would UTF-8-encode U+00FF to
# b"\xc3\xbf" — so the refname travels through argv as raw bytes and
# arrives over git:// as the same raw byte in ls-remote's reply.
_BAD_REF = b"refs/heads/bad\xffb"
# What capture mode must yield for that byte after the fix.
_REPLACED_REF = "bad\ufffdb"

# The capture pipe decodes under the *test process* locale (the child
# env is irrelevant — subprocess.run(text=True) decodes in-process).
# Only under UTF-8-family encodings does a 0xff byte become U+FFFD;
# a single-byte encoding would render it as a real character instead.
try:
    _CAPTURE_UTF8 = (
        codecs.lookup(locale.getpreferredencoding(False)).name == "utf-8")
except LookupError:
    _CAPTURE_UTF8 = False


def _git(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Setup git (str argv). Setup failures are pytest.fail, never
    bare AssertionError."""
    r = subprocess.run(
        ["git", *map(str, args)], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.fail(
            f"setup: git {' '.join(map(str, args))} rc={r.returncode}:\n"
            f"{r.stderr}")
    return r


def _git_raw(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Setup git with byte-exact argv — the only way a literal 0xff
    byte reaches a refname."""
    argv = [a if isinstance(a, bytes) else str(a).encode()
            for a in ("git", *args)]
    r = subprocess.run(argv, cwd=cwd, capture_output=True)
    if r.returncode != 0:
        pytest.fail(f"setup: git raw argv rc={r.returncode}: {r.stderr!r}")
    return r


def _gf_bytes(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """`gf` via subprocess with RAW byte streams.

    The scenario under test has gf itself emitting the upstream's 0xff
    byte — the fetch's `bad\\xffb -> origin/bad\\xffb` echo and
    `origin/HEAD set to bad\\xffb` go straight through stream mode to
    gf's own pipes — so the strict `text=True` capture conftest's `gf`
    uses would decode-fail inside the harness rather than observing the
    candidate. Callers decode with errors="replace", the same read a
    terminal gives.
    """
    return subprocess.run(
        [sys.executable, "-m", "gf", *map(str, args)],
        cwd=cwd, capture_output=True)


def _git_daemon() -> str | None:
    """git-daemon's path (it lives in git's exec-path, not PATH)."""
    r = subprocess.run(
        ["git", "--exec-path"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    exe = Path(r.stdout.strip()) / "git-daemon"
    return str(exe) if os.access(exe, os.X_OK) else None


def _preserves_raw_filenames(base: Path) -> bool:
    """True when a 0xff byte survives a round-trip through `base`'s
    filename namespace. The directory LISTING is the oracle: a
    sanitizing fs may resolve a transliterated lookup back to the
    stored name, so `lexists` on the raw path cannot be trusted."""
    probe_name = b"gf-probe-\xff"
    probe = os.path.join(os.fsencode(base), probe_name)
    try:
        fd = os.open(probe, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    except OSError:
        return False
    kept = probe_name in os.listdir(os.fsencode(base))
    for name in os.listdir(os.fsencode(base)):
        if name.startswith(b"gf-probe-"):
            try:
                os.unlink(os.path.join(os.fsencode(base), name))
            except OSError:
                pass
    return kept


@pytest.fixture
def daemon_site(tmp_path, tmp_path_factory):
    """A real `git daemon --export-all` over a byte-safe scratch dir.

    Returns `(scratch, url_for)` where `url_for("name")` is
    `git://127.0.0.1:<port>/<name>` under `scratch`. Both upstream and
    the cloning parent must live on `scratch`: loose-ref filenames carry
    the raw byte end to end.

    The conftest sandbox gitconfig sets `protocol.allow = never` —
    every non-local transport — so the fixture rewrites the suite's
    $HOME-anchored gitconfig to a permissive one for this scenario only
    (the ambient-env scrub strips GIT_CONFIG_GLOBAL outright, so an env
    swap would never reach a gf-spawned `git`). Skips where the daemon
    binary, a loopback listener, or a byte-preserving filesystem is
    unavailable; the capture-decode claim then rests on the unit pins.
    """
    for base in (tmp_path, tmp_path_factory.mktemp("raw-bytes")):
        if _preserves_raw_filenames(base):
            scratch = base
            break
    else:
        pytest.skip(
            "no byte-preserving scratch filesystem for a raw-0xff "
            "refname")

    daemon = _git_daemon()
    if daemon is None:
        pytest.skip("git-daemon not present in git's exec-path")

    gitconfig = Path(os.environ["HOME"]) / ".gitconfig"
    gitconfig.write_text(
        "[init]\n\tdefaultBranch = master\n"
        "[protocol]\n\tallow = always\n")

    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    except OSError as e:
        pytest.skip(f"loopback bind unavailable: {e}")
    finally:
        sock.close()

    log = open(tmp_path / "git-daemon.log", "wb")
    proc = subprocess.Popen(
        [daemon, "--export-all", "--reuseaddr", "--verbose",
         "--listen=127.0.0.1", f"--port={port}",
         f"--base-path={scratch}"],
        stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 10
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(
                        ("127.0.0.1", port), timeout=0.2):
                    ready = True
                    break
            except OSError:
                time.sleep(0.05)
        if not ready:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
            log.close()
            tail = (tmp_path / "git-daemon.log").read_text()[-400:]
            pytest.skip(
                f"git daemon never accepted a connection: {tail!r}")
        yield scratch, lambda name: f"git://127.0.0.1:{port}/{name}"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        log.close()


def _parent_repo(scratch: Path) -> Path:
    """A real parent repo with one commit — what `gf -C` anchors at."""
    parent = scratch / "parent"
    _git("init", "-q", str(parent))
    _git("-C", parent, "commit", "-qm", "root", "--allow-empty")
    return parent


# ---------------------------------------------------------------------------
# unit pins — the capture pipe itself


class TestCaptureDecode:
    def test_non_utf8_bytes_never_raise(self):
        """Raw 0xff on BOTH pipes: capture returns text, no raise.
        Pre-fix this arm dies inside subprocess.run's strict UTF-8 pipe
        decode — UnicodeDecodeError, not an assertion failure."""
        result = runner.run_command(
            [sys.executable, "-c",
             "import sys; sys.stdout.buffer.write(b'a\\xffb'); "
             "sys.stderr.buffer.write(b'c\\xffd')"],
            os.environ.copy(), mode="capture")
        assert result.returncode == 0
        assert isinstance(result.stdout, str)
        assert isinstance(result.stderr, str)
        # locale-independent: each stray byte decodes to ONE character
        # between the ASCII sentinels — never a raise, never a mangling
        assert len(result.stdout) == 3
        assert result.stdout[0] == "a" and result.stdout[-1] == "b"
        assert len(result.stderr) == 3
        assert result.stderr[0] == "c" and result.stderr[-1] == "d"

    @pytest.mark.skipif(
        not _CAPTURE_UTF8,
        reason="capture pipes decode under a non-UTF-8 locale — "
               "0xff does not map to U+FFFD there")
    def test_non_utf8_bytes_become_replacement_chars(self):
        """The decode contract exactly: each stray 0xff is one U+FFFD
        in the captured str — the same value stream mode produces."""
        result = runner.run_command(
            [sys.executable, "-c",
             "import sys; sys.stdout.buffer.write(b'a\\xffb'); "
             "sys.stderr.buffer.write(b'c\\xffd')"],
            os.environ.copy(), mode="capture")
        assert result.stdout == "a\ufffdb"
        assert result.stderr == "c\ufffdd"

    def test_exit_code_still_propagates_with_non_utf8_output(self):
        """A nonzero exit carrying the bad byte: the code propagates
        unchanged — the decode policy must not swallow the status."""
        result = runner.run_command(
            [sys.executable, "-c",
             "import sys; sys.stdout.buffer.write(b'\\xff'); "
             "sys.exit(3)"],
            os.environ.copy(), mode="capture")
        assert result.returncode == 3
        assert isinstance(result.stdout, str) and result.stdout

    def test_ascii_round_trips_unchanged(self):
        """Control: ordinary output is byte-for-byte stable on either
        side of the fix — text, ordering and a nonzero exit."""
        result = runner.run_command(
            [sys.executable, "-c",
             "import sys; sys.stdout.write('out\\n'); "
             "sys.stderr.write('err\\n'); sys.exit(2)"],
            os.environ.copy(), mode="capture")
        assert result.returncode == 2
        assert result.stdout == "out\n"
        assert result.stderr == "err\n"


# ---------------------------------------------------------------------------
# integration pin — a real upstream advertising a non-UTF-8 refname


class TestCloneNonUtf8RemoteRef:
    def test_daemon_served_bad_refname_dies_in_envelope(
            self, daemon_site):
        """HEAD of the served upstream is `refs/heads/bad\\xffb`: the
        clone's ls-remote and symbolic-ref captures carry the raw byte.
        Post-fix the decoded `bad\\ufffdb` name cannot resolve against
        the fetched `origin/bad\\xffb`, so clone dies rc 1 inside the
        `gf:` envelope (`could not resolve remote branch
        'origin/bad\\ufffdb'`) — pre-fix the same captures raise
        UnicodeDecodeError out of cli.main as a Traceback instead."""
        scratch, url_for = daemon_site
        up = scratch / "upstream"
        _git("init", "-q", "--bare", str(up))
        seed = scratch / "seed"
        _git("init", "-q", str(seed))
        (seed / "a.txt").write_text("payload")
        _git("-C", seed, "add", "a.txt")
        _git("-C", seed, "commit", "-qm", "init")
        _git("-C", seed, "push", "-q", str(up), "master")
        sha = _git("-C", seed, "rev-parse", "master").stdout.strip()
        _git_raw("-C", up, "update-ref", _BAD_REF, sha)
        _git_raw("-C", up, "symbolic-ref", "HEAD", _BAD_REF)

        parent = _parent_repo(scratch)
        r = _gf_bytes("-C", str(parent), "clone", url_for("upstream"))

        out = r.stdout.decode(errors="replace")
        err = r.stderr.decode(errors="replace")
        assert r.returncode == 1, (r.returncode, out, err)
        assert "Traceback" not in err
        assert "UnicodeDecodeError" not in err
        assert any(line.startswith("gf: ") for line in err.splitlines())
        assert ("could not resolve remote branch "
                f"'origin/{_REPLACED_REF}'") in err
