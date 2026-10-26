"""无界面批量剪辑：处理“待剪辑”文件夹里的比赛视频，自动找进球并导出集锦。

给 n8n 之类的外部调度用（见 docs/AUTO_CUT.md），直接在进程内调用 Engine，
流程和桌面端一致：建项目 → 标准分析（自动跟踪篮筐）→ 导出全部候选。
目前不会自动识别投篮人，导出的片段都不分球员；需要按人整理时，
用桌面端打开自动建好的项目再审核、标记球员、重新导出。

子命令：
  start   检查有没有待处理视频，有就在后台启动 run，立即输出一行 JSON；
  status  输出当前/上一次运行的状态（一行 JSON）；
  run     前台逐个处理（start 会在后台调用它）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
for extra in (REPO_ROOT / "src", REPO_ROOT / "engine" / "python"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts"}
DONE_DIR_NAME = "已处理"
# 最近改动过的文件可能还在拷贝中，这次先跳过。
SETTLE_SECONDS = 120
POLL_SECONDS = 5
TERMINAL_STATES = {"completed", "failed", "cancelled"}

# 和桌面端默认项目目录相同，自动建的项目会出现在“最近项目”里。
DEFAULT_PROJECTS_ROOT = Path("~/Movies/BasketballProjects").expanduser()
DEFAULT_STATE_DIR = Path("~/Library/Logs/bhe-auto-cut").expanduser()
# SSH 非交互登录的 PATH 里没有 Homebrew，找不到 ffmpeg/ffprobe。
EXTRA_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def ensure_tool_path() -> None:
    if shutil.which("ffmpeg"):
        return
    current = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join([*EXTRA_TOOL_DIRS, current])


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def pending_videos(inbox: Path, now: float | None = None) -> tuple[list[Path], list[Path]]:
    """返回 (可以处理的视频, 还在拷贝中先跳过的视频)，只看 inbox 顶层。"""
    now = time.time() if now is None else now
    ready: list[Path] = []
    settling: list[Path] = []
    if not inbox.is_dir():
        return ready, settling
    for path in sorted(inbox.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        if now - path.stat().st_mtime < SETTLE_SECONDS:
            settling.append(path)
        else:
            ready.append(path)
    return ready, settling


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法为 {path} 找到可用的文件名")


def recording_date(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")


def output_targets(output_root: Path, date: str, stem: str) -> tuple[Path, Path]:
    """返回 (集锦文件, 单个片段目录)，和桌面端一样按日期分文件夹。"""
    day_dir = output_root / date
    return day_dir / f"{stem}-进球集锦.mp4", day_dir / f"{stem}-进球片段"


def pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StatusFile:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "status.json"

    def read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "idle"}
        return data if isinstance(data, dict) else {"state": "idle"}

    def write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(self.path)


def wait_for_job(
    service: Any,
    project_root: str,
    job_id: str,
    on_progress: Callable[[str, float], None] | None = None,
) -> Dict[str, Any]:
    while True:
        job = service.handle("get_job", {"project_root": project_root, "job_id": job_id})["job"]
        if job.get("state") in TERMINAL_STATES:
            return job
        if on_progress:
            on_progress(str(job.get("stage") or ""), float(job.get("progress") or 0))
        time.sleep(POLL_SECONDS)


def process_video(
    service: Any,
    source: Path,
    output_root: Path,
    projects_root: Path,
    on_progress: Callable[[str, float], None] | None = None,
) -> Dict[str, Any]:
    """处理一个已经移到“已处理”目录里的视频，返回结果摘要。"""
    date = recording_date(source)
    stem = source.stem
    result: Dict[str, Any] = {"file": source.name, "date": date, "goals": 0}
    project_root = unique_path(projects_root / f"{stem}-自动剪辑")
    project_root.mkdir(parents=True)
    root = str(project_root)
    result["project"] = root
    started = time.perf_counter()
    try:
        service.handle("create_project", {"name": stem, "root_path": root})
        video = service.handle("link_video", {"project_root": root, "video_path": str(source)})["video"]
        video_id = video["id"]
        result["duration_s"] = round(int(video.get("duration_ms") or 0) / 1000)
        job = service.handle(
            "start_analysis",
            {"project_root": root, "video_id": video_id, "mode": "standard"},
        )["job"]
        job = wait_for_job(service, root, job["id"], on_progress)
        if job.get("state") != "completed":
            raise RuntimeError(f"分析失败：{job.get('error_message') or job.get('state')}")
        candidates = [
            item
            for item in service.handle("list_candidates", {"project_root": root, "video_id": video_id})["candidates"]
            if item.get("selection_status") != "excluded"
        ]
        result["goals"] = len(candidates)
        if not candidates:
            result["status"] = "no_goals"
            return result
        highlight, clips_dir = output_targets(output_root, date, stem)
        highlight = unique_path(highlight)
        clips_dir = unique_path(clips_dir)
        job = service.handle(
            "start_export",
            {
                "project_root": root,
                "video_id": video_id,
                "mode": "merge",
                "output_dir": str(clips_dir),
                "output_path": str(highlight),
            },
        )["job"]
        job = wait_for_job(service, root, job["id"], on_progress)
        if job.get("state") != "completed":
            raise RuntimeError(f"导出失败：{job.get('error_message') or job.get('state')}")
        (clips_dir / "concat.txt").unlink(missing_ok=True)
        result.update(status="ok", highlight=str(highlight), clips_dir=str(clips_dir))
        return result
    except Exception as exc:  # 单个视频失败不影响后面的视频
        result.update(status="failed", error=str(getattr(exc, "message", None) or exc)[:500])
        return result
    finally:
        result["elapsed_s"] = round(time.perf_counter() - started)
        try:
            # 只清代理、检测缓存，保留 project.db，桌面端还能打开审核。
            service.handle("cleanup_artifacts", {"project_root": root})
        except Exception:
            pass


def summarize(results: list[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "videos": len(results),
        "ok": sum(1 for item in results if item.get("status") == "ok"),
        "no_goals": sum(1 for item in results if item.get("status") == "no_goals"),
        "failed": sum(1 for item in results if item.get("status") == "failed"),
        "goals": sum(int(item.get("goals") or 0) for item in results),
    }


def command_run(args: argparse.Namespace) -> int:
    from basketball_engine.service import EngineService

    status_file = StatusFile(args.state_dir)
    status = status_file.read()
    run_id = args.run_id or uuid.uuid4().hex[:8]
    if status.get("run_id") != run_id:
        status = {"run_id": run_id, "started_at": now_iso()}
    status.update(state="running", pid=os.getpid(), results=[], current=None)
    status_file.write(status)

    inbox: Path = args.inbox
    done_dir = inbox / DONE_DIR_NAME
    ready, settling = pending_videos(inbox)
    status["skipped_copying"] = [path.name for path in settling]
    service = EngineService()
    try:
        for path in ready:
            done_dir.mkdir(exist_ok=True)
            # 先移走再建项目：项目里记的是最终位置，之后桌面端打开不用重新关联，
            # 下次运行也不会重复处理。
            target = unique_path(done_dir / path.name)
            shutil.move(str(path), str(target))
            status.update(current=target.name, stage="", progress=0.0)
            status_file.write(status)

            def on_progress(stage: str, progress: float) -> None:
                if stage != status.get("stage") or abs(progress - float(status.get("progress") or 0)) >= 0.05:
                    status.update(stage=stage, progress=round(progress, 3))
                    status_file.write(status)

            result = process_video(service, target, args.output, args.projects_root, on_progress)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            status["results"].append(result)
            status_file.write(status)
    except Exception as exc:
        status["error"] = str(exc)[:500]
    finally:
        service.shutdown()
        status.update(
            state="done",
            current=None,
            stage=None,
            progress=None,
            finished_at=now_iso(),
            summary=summarize(status["results"]),
        )
        status_file.write(status)
    return 1 if status.get("error") else 0


def command_start(args: argparse.Namespace) -> int:
    status_file = StatusFile(args.state_dir)
    status = status_file.read()
    if status.get("state") == "running" and pid_alive(status.get("pid")):
        print(json.dumps({"started": False, "reason": "running", "status": status}, ensure_ascii=False))
        return 0
    ready, settling = pending_videos(args.inbox)
    if not args.inbox.is_dir():
        print(json.dumps({"started": False, "reason": "inbox_missing", "inbox": str(args.inbox)}, ensure_ascii=False))
        return 0
    if not ready:
        print(json.dumps(
            {"started": False, "reason": "empty", "skipped_copying": [path.name for path in settling]},
            ensure_ascii=False,
        ))
        return 0
    run_id = uuid.uuid4().hex[:8]
    args.state_dir.mkdir(parents=True, exist_ok=True)
    status_file.write({"state": "running", "run_id": run_id, "started_at": now_iso(), "pid": None, "results": []})
    command = [
        sys.executable, str(Path(__file__).resolve()), "run",
        "--run-id", run_id,
        "--inbox", str(args.inbox),
        "--output", str(args.output),
        "--projects-root", str(args.projects_root),
        "--state-dir", str(args.state_dir),
    ]
    caffeinate = shutil.which("caffeinate")
    if caffeinate:
        # 处理期间不让 Mac 睡眠。
        command = [caffeinate, "-i", *command]
    log = (args.state_dir / "auto-cut.log").open("a", encoding="utf-8")
    log.write(f"\n=== {now_iso()} run {run_id} ===\n")
    log.flush()
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(REPO_ROOT),
    )
    print(json.dumps(
        {
            "started": True,
            "run_id": run_id,
            "files": [path.name for path in ready],
            "skipped_copying": [path.name for path in settling],
        },
        ensure_ascii=False,
    ))
    return 0


def command_status(args: argparse.Namespace) -> int:
    status = StatusFile(args.state_dir).read()
    if status.get("state") == "running" and status.get("pid") and not pid_alive(status.get("pid")):
        status["state"] = "crashed"
    print(json.dumps(status, ensure_ascii=False))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("start", "status", "run"))
    parser.add_argument("--inbox", type=Path, help="待剪辑文件夹")
    parser.add_argument("--output", type=Path, help="导出根目录，集锦放在 <output>/<YYYYMMDD>/ 下")
    parser.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT, help="自动建的 BHE 项目放这里")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="状态文件和日志目录")
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command in {"start", "run"} and (args.inbox is None or args.output is None):
        parser.error("start/run 需要 --inbox 和 --output")
    for name in ("inbox", "output", "projects_root", "state_dir"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.expanduser())
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ensure_tool_path()
    handler = {"start": command_start, "status": command_status, "run": command_run}[args.command]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
