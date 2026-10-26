import json
import os
import time
from pathlib import Path

import auto_cut_folder as auto_cut


def _touch(path: Path, age_seconds: float) -> Path:
    path.write_bytes(b"x")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_pending_videos_skips_hidden_non_video_and_copying(tmp_path):
    _touch(tmp_path / "b.MP4", 600)
    _touch(tmp_path / "a.mov", 600)
    _touch(tmp_path / "._a.mov", 600)
    _touch(tmp_path / "notes.txt", 600)
    _touch(tmp_path / "copying.mp4", 10)
    (tmp_path / auto_cut.DONE_DIR_NAME).mkdir()
    _touch(tmp_path / auto_cut.DONE_DIR_NAME / "old.mp4", 600)

    ready, settling = auto_cut.pending_videos(tmp_path)

    assert [path.name for path in ready] == ["a.mov", "b.MP4"]
    assert [path.name for path in settling] == ["copying.mp4"]


def test_pending_videos_missing_inbox(tmp_path):
    assert auto_cut.pending_videos(tmp_path / "missing") == ([], [])


def test_unique_path_adds_suffix(tmp_path):
    (tmp_path / "game.mp4").write_bytes(b"x")
    (tmp_path / "game_2.mp4").write_bytes(b"x")
    assert auto_cut.unique_path(tmp_path / "game.mp4") == tmp_path / "game_3.mp4"
    assert auto_cut.unique_path(tmp_path / "new.mp4") == tmp_path / "new.mp4"


def test_output_targets_group_by_date(tmp_path):
    highlight, clips = auto_cut.output_targets(tmp_path, "20261021", "DJI_0017")
    assert highlight == tmp_path / "20261021" / "DJI_0017-进球集锦.mp4"
    assert clips == tmp_path / "20261021" / "DJI_0017-进球片段"


def test_status_reports_crashed_when_runner_gone(tmp_path, capsys):
    status = auto_cut.StatusFile(tmp_path)
    status.write({"state": "running", "run_id": "abc", "pid": 999999999})

    auto_cut.main(["status", "--state-dir", str(tmp_path)])

    assert json.loads(capsys.readouterr().out)["state"] == "crashed"


def test_start_with_empty_inbox_does_not_launch(tmp_path, capsys):
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    auto_cut.main([
        "start", "--inbox", str(inbox), "--output", str(tmp_path / "out"),
        "--state-dir", str(tmp_path / "state"),
    ])

    assert json.loads(capsys.readouterr().out) == {
        "started": False, "reason": "empty", "skipped_copying": [],
    }
    assert not (tmp_path / "state" / "status.json").exists()


class FakeService:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = []

    def handle(self, command, payload):
        self.calls.append((command, payload))
        if command == "link_video":
            return {"video": {"id": "video_1", "duration_ms": 180000}}
        if command in {"start_analysis", "start_export"}:
            return {"job": {"id": command}}
        if command == "get_job":
            return {"job": {"state": "completed"}}
        if command == "list_candidates":
            return {"candidates": self.candidates}
        if command == "start_export":
            return {"job": {"id": "export"}}
        return {}


def test_process_video_exports_merge_and_cleans_cache(tmp_path):
    source = _touch(tmp_path / "game.mp4", 600)
    service = FakeService([{"id": "c1"}, {"id": "c2", "selection_status": "excluded"}])

    result = auto_cut.process_video(service, source, tmp_path / "out", tmp_path / "projects")

    assert result["status"] == "ok"
    assert result["goals"] == 1
    export = next(payload for command, payload in service.calls if command == "start_export")
    assert export["mode"] == "merge"
    assert export["output_path"].endswith("game-进球集锦.mp4")
    assert service.calls[-1][0] == "cleanup_artifacts"


def test_process_video_without_goals_skips_export(tmp_path):
    source = _touch(tmp_path / "game.mp4", 600)
    service = FakeService([])

    result = auto_cut.process_video(service, source, tmp_path / "out", tmp_path / "projects")

    assert result["status"] == "no_goals"
    assert "start_export" not in [command for command, _ in service.calls]
