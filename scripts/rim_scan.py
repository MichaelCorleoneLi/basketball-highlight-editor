"""Find made baskets without a hand-drawn ROI or net box.

1. Hoop track: one full frame per second through the YOLO model.
2. Crop pass: a hoop-centred crop at 30 fps; YOLO ball/hoop detections and
   pixel change in the net on every frame.
3. Rim events: every ball visit to the rim, scored by the make model.

Prints ``progress=<0..1>`` lines for the engine.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from statistics import median

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from basketball_highlight.rim_events import find_rim_events, load_make_model, make_probability  # noqa: E402

SCAN_VERSION = "rim-scan-v1"
CROP_SIZE = 512
HOOP_IMGSZ = 1280
CROP_IMGSZ = 640


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--make-model", help="make model JSON (default: bundled)")
    parser.add_argument("--device", default="auto", choices=("auto", "cpu", "mps", "cuda"))
    return parser.parse_args()


def select_device(requested):
    if requested != "auto":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda"
    return "mps" if torch.backends.mps.is_available() else "cpu"


def probe(video):
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(video)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    stream = data["streams"][0]
    return int(stream["width"]), int(stream["height"]), float(data["format"]["duration"])


def decode(video, start, end, filters, width, height, pix_fmt="bgr24"):
    """Yield raw frames from ffmpeg (hardware decoding when available)."""
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    channels = 3 if pix_fmt == "bgr24" else 1
    attempts = (["-hwaccel", "videotoolbox"], []) if platform.system() == "Darwin" else ([],)
    for hwaccel in attempts:
        command = [ffmpeg, "-v", "error", *hwaccel, "-ss", f"{start:.3f}"]
        if end is not None:
            command += ["-to", f"{end:.3f}"]
        command += ["-i", str(video), "-an", "-vf", filters, "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        size = width * height * channels
        produced = 0
        while True:
            buffer = process.stdout.read(size)
            if len(buffer) < size:
                break
            produced += 1
            yield np.frombuffer(buffer, np.uint8).reshape(height, width, channels)
        process.wait()
        if produced or process.returncode == 0:
            return
    raise RuntimeError(f"ffmpeg could not decode {video}")


def track_hoop(model, video, start, end, width, height, device, progress):
    duration = (end if end is not None else start) - start
    records = []
    batch = []

    def flush():
        results = model.predict([frame for _, frame in batch], imgsz=HOOP_IMGSZ, conf=0.05, device=device, verbose=False)
        for (second, _), result in zip(batch, results):
            hoops = sorted(
                ([float(box.conf[0]), [round(v, 1) for v in box.xyxy[0].tolist()]]
                 for box in result.boxes if int(box.cls[0]) == 1),
                reverse=True,
            )
            records.append({"t": start + second, "hoops": hoops[:3]})
        batch.clear()

    for index, frame in enumerate(decode(video, start, end, "fps=1", width, height)):
        batch.append((index, frame))
        if len(batch) == 16:
            flush()
            if duration > 0:
                progress(min(1.0, index / duration))
    if batch:
        flush()
    return records


def hoop_boxes(records):
    boxes = [(r["t"], r["hoops"][0][1]) for r in records if r["hoops"] and r["hoops"][0][0] >= 0.3]
    if len(boxes) < 5:
        raise RuntimeError("NO_HOOP_FOUND: 视频中没有稳定识别到篮筐")
    return boxes


def crop_geometry(boxes, width, height):
    xs = np.array([(b[0] + b[2]) / 2 for _, b in boxes])
    ys = np.array([(b[1] + b[3]) / 2 for _, b in boxes])
    x_lo, x_hi = np.percentile(xs, 2), np.percentile(xs, 98)
    y_lo, y_hi = np.percentile(ys, 2), np.percentile(ys, 98)
    w = int(min(width, max(CROP_SIZE, x_hi - x_lo + CROP_SIZE * 0.75)))
    h = int(min(height, max(CROP_SIZE, y_hi - y_lo + CROP_SIZE * 0.75)))
    w -= w % 2
    h -= h % 2
    x0 = int(np.clip((x_lo + x_hi) / 2 - w / 2, 0, width - w))
    y0 = int(np.clip((y_lo + y_hi) / 2 - h * 0.6, 0, height - h))
    return x0, y0, w, h


def hoop_in_crop(boxes, x0, y0):
    """Per-time hoop box (cx, top, w, h) in crop pixels: ±3 s median of the hoop track."""
    times = np.array([t for t, _ in boxes])
    values = np.array([b for _, b in boxes])
    overall = np.median(values, axis=0)

    def at(t):
        mask = (times >= t - 3) & (times <= t + 3)
        b = np.median(values[mask], axis=0) if mask.sum() >= 2 else overall
        return ((b[0] + b[2]) / 2 - x0, b[1] - y0, b[2] - b[0], b[3] - b[1])

    return at


def net_motion(gray, previous, hoop):
    cx, top, rim_w, rim_h = hoop
    x1, x2 = max(0, int(cx - 0.45 * rim_w)), max(1, int(cx + 0.45 * rim_w))
    regions = (
        (int(top + 0.25 * rim_h), int(top + rim_h)),
        (int(top + rim_h), int(top + rim_h + 0.8 * rim_w)),
    )
    if previous is None:
        return [0.0, 0.0]
    diff = np.abs(gray.astype(np.int16) - previous.astype(np.int16))
    values = []
    for y1, y2 in regions:
        region = diff[max(0, y1):max(1, y2), x1:x2]
        values.append(round(float((region > 20).mean()), 4) if region.size else 0.0)
    return values


def crop_pass(model, video, start, end, geometry, hoop_at, fps, device, progress):
    import cv2

    x0, y0, w, h = geometry
    duration = (end if end is not None else start) - start
    expected = max(1, int(duration * fps))
    frames = []
    motion = []
    previous = None
    batch = []

    def flush():
        results = model.predict(batch, imgsz=CROP_IMGSZ, conf=0.08, device=device, verbose=False)
        for result in results:
            frames.append([
                [int(box.cls[0]), round(float(box.conf[0]), 3), *[round(v, 1) for v in box.xyxy[0].tolist()]]
                for box in result.boxes
            ])
        batch.clear()

    for index, frame in enumerate(decode(video, start, end, f"fps={fps},crop={w}:{h}:{x0}:{y0}", w, h)):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        motion.append(net_motion(gray, previous, hoop_at(start + index / fps)))
        previous = gray
        batch.append(frame)
        if len(batch) == 32:
            flush()
            progress(min(1.0, index / expected))
    if batch:
        flush()
    return frames, motion


def main(args):
    started = time.perf_counter()
    video = Path(args.video).resolve()
    width, height, duration = probe(video)
    start = max(0.0, args.start)
    end = min(duration, args.end) if args.end else duration
    cache = None
    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        stat = video.stat()
        key = f"{SCAN_VERSION}-{stat.st_size}-{stat.st_mtime_ns}-{start:.2f}-{end:.2f}-{args.fps}"
        cache = args.cache_dir / f"{key}.json"

    def progress(value):
        print(f"progress={value:.4f}", flush=True)

    progress(0.0)
    if cache and cache.is_file():
        scan = json.loads(cache.read_text(encoding="utf-8"))
    else:
        from ultralytics import YOLO

        device = select_device(args.device)
        model = YOLO(str(Path(args.model).resolve()))
        records = track_hoop(model, video, start, end, width, height, device, lambda v: progress(0.15 * v))
        boxes = hoop_boxes(records)
        geometry = crop_geometry(boxes, width, height)
        frames, motion = crop_pass(
            model, video, start, end, geometry, hoop_in_crop(boxes, *geometry[:2]), args.fps, device,
            lambda v: progress(0.15 + 0.8 * v),
        )
        scan = {
            "version": SCAN_VERSION,
            "fps": args.fps,
            "start": start,
            "end": end,
            "crop": dict(zip(("x0", "y0", "w", "h"), geometry)),
            "hoop_track": records,
            "frames": frames,
            "net_motion": motion,
        }
        if cache:
            temporary = cache.with_suffix(".tmp")
            temporary.write_text(json.dumps(scan, separators=(",", ":")), encoding="utf-8")
            temporary.replace(cache)

    make_model = load_make_model(args.make_model) if args.make_model else load_make_model()
    crop = scan["crop"]
    events = find_rim_events(scan["frames"], scan["fps"], time_offset=scan["start"], net_motion=scan["net_motion"])
    for event in events:
        event["make_probability"] = round(make_probability(event["features"], make_model), 4)
        hoop = event["hoop"]
        event["hoop_source"] = {
            "center_x": round(hoop["center_x"] + crop["x0"], 1),
            "top": round(hoop["top"] + crop["y0"], 1),
            "width": round(hoop["width"], 1),
            "height": round(hoop["height"], 1),
        }
    boxes = hoop_boxes(scan["hoop_track"])
    hoop_median = [round(median(b[i] for _, b in boxes), 1) for i in range(4)]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "version": SCAN_VERSION,
        "video": str(video),
        "width": width,
        "height": height,
        "start": scan["start"],
        "end": scan["end"],
        "crop": crop,
        "hoop_bbox": hoop_median,
        "events": events,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    progress(1.0)
    print(f"events={len(events)} makes={sum(e['make_probability'] >= 0.5 for e in events)}")


if __name__ == "__main__":
    main(parse_args())
