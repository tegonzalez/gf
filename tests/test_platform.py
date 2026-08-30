import os
from pathlib import Path

from gf import platform


def test_same_path_for_identical_file(tmp_path: Path) -> None:
    p = tmp_path / "a"
    p.write_text("x")
    assert platform.same_path(p, p) is True


def test_same_path_for_symlink_and_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert platform.same_path(target, link) is True


def test_same_path_for_different_files(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_text("x")
    b.write_text("y")
    assert platform.same_path(a, b) is False


def test_same_path_for_resolved_nonexistent_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target"
    link = tmp_path / "link"
    link.symlink_to(target)
    other = tmp_path / "other"
    other.symlink_to(target)
    assert platform.same_path(link, other) is True


def test_same_path_for_missing_placeholder_spellings(tmp_path: Path) -> None:
    child = tmp_path / "vendor" / "lib"
    via_url = tmp_path / "vendor" / ".." / "vendor" / "lib"
    assert platform.same_path(child, via_url) is True


def test_logical_cwd_prefers_pwd_when_same_directory(
    tmp_path: Path, monkeypatch
) -> None:
    original = os.getcwd()
    try:
        os.chdir(tmp_path)
        monkeypatch.setenv("PWD", str(tmp_path))
        assert platform.logical_cwd() == tmp_path
    finally:
        os.chdir(original)


def test_logical_cwd_falls_back_to_getcwd_when_pwd_differs(
    tmp_path: Path, monkeypatch
) -> None:
    original = os.getcwd()
    try:
        os.chdir(tmp_path)
        monkeypatch.setenv("PWD", "/not/the/same/directory")
        assert platform.logical_cwd() == Path(tmp_path)
    finally:
        os.chdir(original)


def test_logical_cwd_rejects_relative_pwd(tmp_path: Path, monkeypatch) -> None:
    original = os.getcwd()
    try:
        os.chdir(tmp_path)
        monkeypatch.setenv("PWD", ".")
        assert platform.logical_cwd() == tmp_path
    finally:
        os.chdir(original)


def test_exec_or_run_calls_execvpe(tmp_path: Path, monkeypatch) -> None:
    calls = []
    original = os.getcwd()

    def fake_execvpe(file, args, env):
        calls.append((file, list(args), dict(env), os.getcwd()))

    monkeypatch.setattr(os, "execvpe", fake_execvpe)
    try:
        env = {"A": "1"}
        platform.exec_or_run(["true"], env, cwd=tmp_path)
    finally:
        os.chdir(original)
    assert calls == [("true", ["true"], {"A": "1"}, str(tmp_path))]
