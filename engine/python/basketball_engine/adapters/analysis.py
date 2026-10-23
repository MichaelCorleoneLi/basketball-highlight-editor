from __future__ import annotations

import sys
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from uuid import uuid5, NAMESPACE_URL

# The desktop runtime puts the engine and the shared analysis package in
# sibling directories under the bundled runtime root.
_RUNTIME_ROOT = Path(__file__).resolve().parents[4]
_SRC_PATH = _RUNTIME_ROOT / "src"
if _SRC_PATH.is_dir() and str(_SRC_PATH) not in sys.path:
    sys.path.insert(0, str(_SRC_PATH))

from basketball_highlight.review_reason import suggest_review_reasons
from basketball_highlight.ranking import candidate_priority, dedupe_candidates


class PipelineCancelled(Exception):
    pass


def terminate_process(process: subprocess.Popen) -> None:
    """Stop a pipeline step and its child process group."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
            )
        else:
            os.killpg(process.pid, 15)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
            else:
                os.killpg(process.pid, 9)
        except (OSError, ProcessLookupError):
            process.kill()
        process.wait(timeout=2)


def scale_roi_to_proxy(
    roi: Dict[str, Any],
    source_width: int,
    proxy_width: int,
    *,
    source_height: int | None = None,
    proxy_height: int | None = None,
) -> list[int]:
    if source_width <= 0 or proxy_width <= 0:
        raise ValueError("VIDEO_DIMENSION_INVALID")
    if (source_height is None) != (proxy_height is None):
        raise ValueError("VIDEO_DIMENSION_INVALID")
    if source_height is not None and proxy_height is not None:
        if source_height <= 0 or proxy_height <= 0:
            raise ValueError("VIDEO_DIMENSION_INVALID")
        ratio = min(proxy_width / source_width, proxy_height / source_height)
    else:
        ratio = proxy_width / source_width
    return [
        round(float(roi[key]) * ratio)
        for key in ("x1", "y1", "x2", "y2")
    ]


def flatten_refined_matches(data: Dict[str, Any], dedupe_seconds: float = 2.0) -> list[Dict[str, Any]]:
    matches = [
        dict(match)
        for result in data.get("results", [])
        for match in result.get("refined", [])
        if (
            isinstance(match, dict)
            and "time" in match
            and match.get("verdict") != "missed"
        )
    ]
    return dedupe_candidates(matches, dedupe_seconds)


def flatten_coarse_matches(data: Dict[str, Any], dedupe_seconds: float = 2.0) -> list[Dict[str, Any]]:
    """Normalize coarse candidates for the fast path.

    Coarse candidates already use source-video timestamps (``scan_video``
    applies the analysis-range offset), so the fast path only needs the same
    conservative de-duplication used by the refined path. Preserve the two
    detections that formed the crossing so the review screen can explain that
    this is a coarse result instead of displaying ``pending`` with no evidence.
    """
    matches = []
    for candidate in data.get("candidates", []):
        if not isinstance(candidate, dict) or "time" not in candidate:
            continue
        normalized = dict(candidate)
        above = normalized.get("above") if isinstance(normalized.get("above"), dict) else {}
        below = normalized.get("below") if isinstance(normalized.get("below"), dict) else {}
        confidences = [
            float(value)
            for value in (above.get("confidence"), below.get("confidence"))
            if isinstance(value, (int, float))
        ]
        if confidences:
            score = round(sum(confidences) / len(confidences), 3)
            normalized["coarse_detection_confidence"] = score
            normalized["score"] = normalized.get("score") or score
            raw_confidence = normalized.get("confidence")
            if raw_confidence is None or str(raw_confidence).lower() == "pending":
                normalized["confidence"] = (
                    "high" if score >= 0.65 else "medium" if score >= 0.4 else "low"
                )
        if confidences:
            normalized["coarse_crossing"] = True
        matches.append(normalized)
    return dedupe_candidates(matches, dedupe_seconds)


# 同一个进球常同时产生精扫和粗扫候选，粗扫时间会偏 2~5 秒；片段前后
# 各留数秒，两个候选会剪出同一个球。同一篮筐 5 秒内不会有两次进球。
SAME_SHOT_SECONDS = 5.0
# 合并后的片段最多跨越这么久，避免连续的补篮/拼抢串成一个超长片段。
MAX_MERGED_SPAN_SECONDS = 10.0


def merge_same_shot_matches(
    matches: list[Dict[str, Any]],
    merge_seconds: float = SAME_SHOT_SECONDS,
) -> list[Dict[str, Any]]:
    """Merge candidates that would cut the same shot into one clip.

    The strongest candidate (refined over coarse, made over ambiguous) keeps
    its event time. ``clip_start_time``/``clip_end_time`` record the earliest
    and latest merged event so the clip still covers every merged moment,
    and no basket is lost when a miss and its putback merge.
    """
    merged: list[Dict[str, Any]] = []
    for match in sorted(matches, key=lambda item: float(item["time"])):
        time_value = float(match["time"])
        if (
            merged
            and time_value - float(merged[-1]["clip_end_time"]) <= merge_seconds
            and time_value - float(merged[-1]["clip_start_time"]) <= MAX_MERGED_SPAN_SECONDS
        ):
            current = merged[-1]
            start = float(current["clip_start_time"])
            winner = match if candidate_priority(match) > candidate_priority(current) else current
            merged[-1] = {
                **winner,
                "clip_start_time": start,
                "clip_end_time": time_value,
                "merged_times": [*current["merged_times"], time_value],
            }
            continue
        merged.append({
            **match,
            "clip_start_time": time_value,
            "clip_end_time": time_value,
            "merged_times": [time_value],
        })
    for match in merged:
        if len(match["merged_times"]) == 1:
            for key in ("clip_start_time", "clip_end_time", "merged_times"):
                match.pop(key)
    return merged


# 低于这个进球概率的篮筐事件不进入审核列表。在 9 个已标注视频上留一个
# 视频交叉验证：0.35 时找回约 94% 的进球，候选数约为旧版的一半。
RIM_MAKE_THRESHOLD = 0.35


def rim_scan_command(
    repo_root: Path,
    source_video: Path,
    model_path: Path,
    output: Path,
    cache_dir: Path,
    analysis_start_ms: int = 0,
    analysis_end_ms: int | None = None,
) -> list[str]:
    command = [
        sys.executable, _script(repo_root, "rim_scan.py"),
        "--video", str(source_video), "--model", str(model_path),
        "--output", str(output), "--cache-dir", str(cache_dir),
        "--start", str(max(0.0, analysis_start_ms / 1000.0)),
    ]
    if analysis_end_ms:
        command += ["--end", str(analysis_end_ms / 1000.0)]
    return command


def rim_events_to_matches(
    scan: Dict[str, Any],
    threshold: float = RIM_MAKE_THRESHOLD,
) -> list[Dict[str, Any]]:
    """Turn rim-scan events into candidate matches for ``candidate_to_row``."""
    matches = []
    for event in scan.get("events", []):
        probability = float(event.get("make_probability", 0.0))
        if probability < threshold:
            continue
        hoop = event.get("hoop_source") or {}
        rim = {
            "center_x": hoop.get("center_x"),
            "rim_y": hoop.get("top"),
            "width": hoop.get("width"),
            "height": hoop.get("height"),
        }
        matches.append({
            "time": float(event["time"]),
            "score": round(probability, 3),
            "make_probability": round(probability, 3),
            "confidence": "high" if probability >= 0.8 else "medium" if probability >= 0.5 else "low",
            "verdict": "made" if probability >= 0.8 else "ambiguous",
            "rim_features": event.get("features", {}),
            "rim": rim,
            "overlay": {"rim": rim, "source": "rim_scan"},
        })
    return matches


def candidate_to_row(
    match: Dict[str, Any],
    video_id: str,
    roi_id: str,
    duration_ms: int,
    before_seconds: float,
    after_seconds: float,
    detector_version: str,
    analysis_source: str | None = None,
) -> Dict[str, Any]:
    event_time_ms = round(float(match["time"]) * 1000)
    clip_start_ms = round(float(match.get("clip_start_time", match["time"])) * 1000)
    clip_end_ms = round(float(match.get("clip_end_time", match["time"])) * 1000)
    start_ms = max(0, min(event_time_ms, clip_start_ms) - round(before_seconds * 1000))
    end_ms = min(duration_ms, max(event_time_ms, clip_end_ms) + round(after_seconds * 1000))
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    evidence = dict(match)
    if analysis_source:
        evidence["analysis_source"] = analysis_source
    evidence["review_reason_suggestion"] = suggest_review_reasons(match)
    return {
        "id": f"candidate_{uuid5(NAMESPACE_URL, f'{video_id}:{roi_id}:{detector_version}:{event_time_ms}').hex}",
        "video_id": video_id,
        "roi_id": roi_id,
        "group_id": None,
        "event_time_ms": event_time_ms,
        "default_start_ms": start_ms,
        "default_end_ms": end_ms,
        "review_start_ms": start_ms,
        "review_end_ms": end_ms,
        "detector_version": detector_version,
        "score": match.get("score"),
        "confidence": match.get("confidence", "pending"),
        "evidence_json": json.dumps(evidence, ensure_ascii=False),
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _script(repo_root: Path, name: str) -> str:
    return str((repo_root / "scripts" / name).resolve())


def build_pipeline_commands(
    repo_root: Path,
    source_video: Path,
    proxy_video: Path,
    model_path: Path,
    coarse_detections: Path,
    coarse_candidates: Path,
    refined_output: Path,
    proxy_roi: Iterable[int],
    source_roi: Iterable[int],
    cache_dir: Path,
    proxy_width: int = 960,
    proxy_height: int = 720,
    proxy_fps: float = 5.0,
    proxy_scale: float = 1.0,
    coarse_scale: int = 4,
    refine_sample_fps: float = 10.0,
    refine_scale: int = 2,
    batch: int = 8,
    conf: float = 0.10,
    window_seconds: float = 2.5,
    analysis_start_ms: int = 0,
    analysis_end_ms: int | None = None,
    net_roi: Iterable[float] | None = None,
    include_refinement: bool = True,
) -> list[list[str]]:
    proxy_roi_args = [str(round(float(value))) for value in proxy_roi]
    source_roi_args = [str(round(float(value))) for value in source_roi]
    net_roi_args = [str(round(float(value))) for value in net_roi] if net_roi else []
    analysis_start_seconds = max(0.0, analysis_start_ms / 1000.0)
    analysis_duration_seconds = (
        None if analysis_end_ms is None else max(0.0, (analysis_end_ms - analysis_start_ms) / 1000.0)
    )
    proxy_range_args = ["--start-time", str(analysis_start_seconds)]
    if analysis_duration_seconds is not None:
        proxy_range_args += ["--duration", str(analysis_duration_seconds)]
    commands = [
        [
            sys.executable, _script(repo_root, "create_proxy.py"),
            "--video", str(source_video), "--output", str(proxy_video),
            "--width", str(proxy_width), "--height", str(proxy_height),
            "--fps", str(proxy_fps),
            *proxy_range_args,
        ],
        [
            sys.executable, _script(repo_root, "scan_video.py"),
            "--video", str(proxy_video), "--model", str(model_path),
            "--roi", *proxy_roi_args, "--sample-fps", str(proxy_fps),
            "--scale", str(coarse_scale), "--batch", str(batch),
            "--conf", str(conf), "--cache-dir", str(cache_dir / "coarse"),
            "--time-offset", str(analysis_start_seconds),
            "--output", str(coarse_detections),
        ],
        [
            sys.executable, _script(repo_root, "generate_candidates.py"),
            "--detections", str(coarse_detections), "--output", str(coarse_candidates),
        ],
    ]
    if include_refinement:
        commands.append([
            sys.executable, _script(repo_root, "refine_dynamic_candidates.py"),
            "--video", str(source_video), "--model", str(model_path),
            "--coarse", str(coarse_candidates), "--roi", *source_roi_args,
            "--proxy-scale", str(proxy_scale), "--sample-fps", str(refine_sample_fps),
            "--window", str(window_seconds),
            "--scale", str(refine_scale), "--conf", str(conf),
            "--batch", str(batch), "--cache-dir", str(cache_dir / "refine"),
            *( ["--net-roi", *net_roi_args] if net_roi_args else [] ),
            "--output", str(refined_output),
        ])
    return commands


def run_pipeline(
    commands: list[list[str]],
    refined_output: Path,
    stage_callback=None,
    output_callback=None,
    cancel_check=None,
    process_callback=None,
    manifest_path: Path | None = None,
    stage_outputs: Iterable[Path | None] | None = None,
    manifest_version: str | int = 1,
    stage_names: Iterable[str] | None = None,
) -> Dict[str, Any]:
    stages = tuple(stage_names) if stage_names else (
        "prepare_proxy", "coarse_scan", "generate_candidates", "refine_candidates",
    )
    if len(commands) == 4:
        stage_ranges = (
            (0.01, 0.14),
            (0.14, 0.44),
            (0.44, 0.46),
            (0.46, 0.95),
        )
    else:
        stage_width = 0.94 / max(1, len(commands))
        stage_ranges = tuple(
            (0.01 + index * stage_width, 0.01 + (index + 1) * stage_width)
            for index in range(len(commands))
        )
    logs: list[str] = []
    cache_hits = 0
    stage_timings_ms: dict[str, int] = {}
    outputs = list(stage_outputs or [None] * len(commands))
    if len(outputs) != len(commands):
        raise ValueError("PIPELINE_STAGE_OUTPUTS_INVALID")

    manifest: Dict[str, Any] = {"version": manifest_version, "stages": {}}
    if manifest_path and manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                isinstance(loaded, dict)
                and loaded.get("version") == manifest_version
                and isinstance(loaded.get("stages"), dict)
            ):
                manifest.update(loaded)
        except (OSError, json.JSONDecodeError):
            # A truncated manifest must not make an otherwise recoverable run
            # fail. The next completed stage will replace it atomically.
            pass

    def save_manifest() -> None:
        if not manifest_path:
            return
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(manifest_path)

    def artifact_valid(path: Path | None) -> bool:
        if path is None:
            return True
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        if path.suffix.lower() != ".json":
            return True
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return True

    for index, command in enumerate(commands):
        if cancel_check and cancel_check():
            raise PipelineCancelled("JOB_CANCELLED")
        stage = stages[index] if index < len(stages) else f"stage_{index + 1}"
        stage_started = time.perf_counter()
        output = outputs[index]
        previous = manifest.get("stages", {}).get(stage, {})
        if (
            isinstance(previous, dict)
            and previous.get("state") == "completed"
            and artifact_valid(output)
        ):
            cache_hits += 1
            stage_timings_ms[stage] = round((time.perf_counter() - stage_started) * 1000)
            logs.append(f"{stage}: reused {output}")
            if stage_callback:
                stage_callback(stage, stage_ranges[index][1])
            continue
        if stage_callback:
            stage_callback(stage, stage_ranges[index][0])
        manifest.setdefault("stages", {})[stage] = {
            "state": "running",
            "started_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "output": str(output) if output else None,
        }
        save_manifest()
        popen_kwargs = {
            # 子进程绝不能继承引擎的 stdin:那是 Flutter 宿主的 JSONL
            # 请求管道,ffmpeg/脚本抢读会吞掉请求字节甚至导致引擎退出。
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **popen_kwargs)
        if process_callback:
            process_callback(process)
        try:
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def read_stream(stream, lines: list[str]) -> None:
                for line in iter(stream.readline, ""):
                    lines.append(line)
                    stripped = line.strip()
                    if stripped.startswith("progress=") and stage_callback:
                        try:
                            local_progress = float(stripped.split("=", 1)[1])
                        except ValueError:
                            local_progress = None
                        if local_progress is not None:
                            start_progress, end_progress = stage_ranges[index]
                            global_progress = start_progress + (
                                end_progress - start_progress
                            ) * max(0.0, min(1.0, local_progress))
                            stage_callback(stage, max(0.01, global_progress))
                    if output_callback:
                        output_callback(stage, stripped)
                stream.close()

            stdout_reader = threading.Thread(
                target=read_stream,
                args=(process.stdout, stdout_lines),
                name=f"pipeline-stdout-{index}",
                daemon=True,
            )
            stderr_reader = threading.Thread(
                target=read_stream,
                args=(process.stderr, stderr_lines),
                name=f"pipeline-stderr-{index}",
                daemon=True,
            )
            stdout_reader.start()
            stderr_reader.start()
            while process.poll() is None:
                if cancel_check and cancel_check():
                    terminate_process(process)
                    stdout_reader.join(timeout=3)
                    stderr_reader.join(timeout=3)
                    raise PipelineCancelled("JOB_CANCELLED")
                time.sleep(0.05)
            process.wait()
            stdout_reader.join(timeout=3)
            stderr_reader.join(timeout=3)
            stdout = "".join(stdout_lines)
            stderr = "".join(stderr_lines)
            if cancel_check and cancel_check():
                raise PipelineCancelled("JOB_CANCELLED")
            if process.returncode != 0:
                details = (stderr or stdout or "").strip()
                if len(details) > 1200:
                    details = details[-1200:]
                script_name = (
                    Path(command[1]).name
                    if len(command) > 1 and command[1].endswith(".py")
                    else "pipeline step"
                )
                message = f"{script_name} 执行失败"
                if details:
                    message = f"{message}: {details}"
                raise RuntimeError(message)
            completed_stdout = stdout or ""
            completed_stderr = stderr or ""
            if not artifact_valid(output):
                raise RuntimeError(f"{stage} 未生成有效产物")
            manifest.setdefault("stages", {})[stage] = {
                "state": "completed",
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "output": str(output) if output else None,
            }
            save_manifest()
        except PipelineCancelled:
            manifest.setdefault("stages", {})[stage] = {
                "state": "cancelled",
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "output": str(output) if output else None,
            }
            save_manifest()
            raise
        except Exception as exc:
            manifest.setdefault("stages", {})[stage] = {
                "state": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "output": str(output) if output else None,
                "error": str(exc),
            }
            save_manifest()
            raise
        finally:
            if process_callback:
                process_callback(None)
        if completed_stdout:
            logs.append(completed_stdout[-4000:])
        if completed_stderr:
            logs.append(completed_stderr[-4000:])
        stage_timings_ms[stage] = round((time.perf_counter() - stage_started) * 1000)
    if cancel_check and cancel_check():
        raise PipelineCancelled("JOB_CANCELLED")
    if stage_callback:
        stage_callback("persist_candidates", 0.95)
    return {
        "refined": json.loads(refined_output.read_text(encoding="utf-8")),
        "logs": logs,
        "manifest": manifest,
        "cache_hits": cache_hits,
        "stage_timings_ms": stage_timings_ms,
    }
