"""Tests for the shared command runner."""
# SPDX-FileCopyrightText: 2026 Tomas Gonzalez
# SPDX-License-Identifier: MIT

import os
import sys
from unittest.mock import patch

from gf import runner


def test_capture_mode_returns_output():
    result = runner.run_command(["echo", "hello"], os.environ.copy(), mode="capture")
    assert result.returncode == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""


def test_capture_mode_returns_error_for_missing_command():
    result = runner.run_command(["nonexistent_xyz"], os.environ.copy(), mode="capture")
    assert result.returncode == 127
    assert "nonexistent_xyz: command not found" in result.stderr


def test_stream_mode_streams_and_returns_output(capsys):
    result = runner.run_command(["echo", "hi"], os.environ.copy(), mode="stream")
    captured = capsys.readouterr()
    assert result.returncode == 0
    assert result.stdout == "hi\n"
    assert result.stderr == ""
    assert captured.out == "hi\n"


def test_stream_mode_streams_stderr(capsys):
    result = runner.run_command(
        [sys.executable, "-c", "import sys; sys.stderr.write('err\\n')"],
        os.environ.copy(),
        mode="stream",
    )
    captured = capsys.readouterr()
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == "err\n"
    assert captured.err == "err\n"


def test_stream_mode_returns_error_for_missing_command():
    result = runner.run_command(["nonexistent_xyz"], os.environ.copy(), mode="stream")
    assert result.returncode == 127
    assert "nonexistent_xyz: command not found" in result.stderr


def test_exec_mode_returns_error_for_missing_command():
    result = runner.run_command(["nonexistent_xyz"], os.environ.copy(), mode="exec")
    assert result.returncode == 127
    assert "nonexistent_xyz: command not found" in result.stderr


def test_exec_mode_calls_execvpe_for_existing_command():
    calls = []

    def fake_execvpe(file, args, env):
        calls.append((file, args, env))

    with patch("os.execvpe", fake_execvpe):
        result = runner.run_command(["true"], os.environ.copy(), mode="exec")
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
