from pathlib import Path
import subprocess

from scripts.check_runtime import _check_binary, _check_model_load


def _write_tool(path: Path, version: str) -> None:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n", encoding="utf-8")
    path.chmod(0o755)


def test_check_binary_rejects_ffmpeg_disguised_as_ffprobe(tmp_path: Path):
    tool = tmp_path / "ffprobe"
    _write_tool(tool, "ffmpeg version 8.0")

    ok, detail = _check_binary(str(tool), "ffprobe")

    assert not ok
    assert "not ffprobe" in detail


def test_check_binary_accepts_expected_tool(tmp_path: Path):
    tool = tmp_path / "ffprobe"
    _write_tool(tool, "ffprobe version 8.0")

    ok, detail = _check_binary(str(tool), "ffprobe")

    assert ok
    assert detail == str(tool.resolve())


def test_model_load_failure_is_reported(monkeypatch, tmp_path: Path):
    model = tmp_path / "model.pt"
    model.write_bytes(b"model")

    def fail(command, **kwargs):
        assert command[-1] == str(model)
        raise subprocess.CalledProcessError(1, command, stderr="No module named 'dill'")

    monkeypatch.setattr(subprocess, "run", fail)
    ok, detail = _check_model_load("python3", model)

    assert not ok
    assert "dill" in detail
