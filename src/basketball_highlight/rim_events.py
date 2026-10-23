"""Rim events: every time a ball reaches the rim, described for the make model.

Input is the per-frame YOLO output of a hoop-centred crop sampled at a fixed
rate: ``frames[i]`` is a list of ``[class, confidence, x1, y1, x2, y2]`` with
class 0 = ball and 1 = hoop, in crop pixels. Nothing here needs a manually
drawn ROI or net box; the hoop position comes from the detector itself.

The strongest single signal is how much the detector's hoop box changes while
the ball is in the net: the box covers rim and net, and a ball dropping
through stretches the net, so the box grows and shifts. A shot that bounces
off or passes in front of the rim leaves the box almost unchanged.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Sequence

BALL = 0
HOOP = 1
MODEL_PATH = Path(__file__).with_name("make_model.json")

Frame = Sequence[Sequence[float]]


def _center(d):
    return (d[2] + d[4]) / 2.0, (d[3] + d[5]) / 2.0


def drop_static_balls(frames: list[Frame], min_share: float = 0.03) -> tuple[list[list], set]:
    """Remove ball detections that sit on one spot for much of the video.

    Lamps and round signs near the rim are detected as a ball in thousands of
    frames. A real ball never stays in one 16px cell for 3% of a game.
    """
    cells: Counter = Counter()
    for frame in frames:
        seen = {
            (round(_center(d)[0] / 16), round(_center(d)[1] / 16))
            for d in frame
            if d[0] == BALL
        }
        cells.update(seen)
    static = {cell for cell, count in cells.items() if count >= min_share * max(1, len(frames))}

    def is_static(d):
        x, y = _center(d)
        return any(abs(x / 16 - a) <= 1.0 and abs(y / 16 - b) <= 1.0 for a, b in static)

    cleaned = [
        [list(d) for d in frame if not (d[0] == BALL and is_static(d))]
        for frame in frames
    ]
    return cleaned, static


def hoop_series(frames: list[Frame], fps: float) -> list[tuple[float, float, float, float]]:
    """Per-frame hoop (center x, top y, width, height): rolling median over ±2 s."""
    observed: list[tuple[int, tuple[float, float, float, float]]] = []
    for index, frame in enumerate(frames):
        hoops = [d for d in frame if d[0] == HOOP and d[1] >= 0.4]
        if hoops:
            d = max(hoops, key=lambda item: item[1])
            observed.append((index, ((d[2] + d[4]) / 2, d[3], d[4] - d[2], d[5] - d[3])))
    if not observed:
        raise ValueError("NO_HOOP_DETECTIONS")

    def med(values):
        return tuple(median(column) for column in zip(*values))

    overall = med([value for _, value in observed])
    half = int(round(2 * fps))
    series = []
    lo = hi = 0
    for index in range(len(frames)):
        while lo < len(observed) and observed[lo][0] < index - half:
            lo += 1
        while hi < len(observed) and observed[hi][0] <= index + half:
            hi += 1
        window = [value for _, value in observed[lo:hi]]
        series.append(med(window) if len(window) >= 5 else overall)
    return series


def ball_tracks(frames: list[Frame], hoop) -> list[dict]:
    """Greedy constant-velocity linking of ball detections."""
    live: list[dict] = []
    done: list[dict] = []
    for index, frame in enumerate(frames):
        rim_width = hoop[index][2]
        points = [
            (*_center(d), d[4] - d[2], d[1])
            for d in frame
            if d[0] == BALL and d[1] >= 0.15
        ]
        used: set[int] = set()
        for track in sorted(live, key=lambda item: -item["pts"][-1][0]):
            last_index, last_x, last_y, last_size, _ = track["pts"][-1]
            gap = index - last_index
            vx, vy = track.get("v", (0.0, 0.0))
            px, py = last_x + vx * gap, last_y + vy * gap
            best, best_distance = None, math.inf
            for k, (x, y, _size, _conf) in enumerate(points):
                if k in used:
                    continue
                distance = math.hypot(x - px, y - py)
                if distance < max(1.2 * rim_width, 2.5 * last_size) * min(gap, 3) and distance < best_distance:
                    best, best_distance = k, distance
            if best is not None:
                used.add(best)
                x, y, size, conf = points[best]
                track["v"] = ((x - last_x) / gap, (y - last_y) / gap)
                track["pts"].append((index, x, y, size, conf))
        for k, (x, y, size, conf) in enumerate(points):
            if k not in used:
                live.append({"pts": [(index, x, y, size, conf)]})
        still_live = []
        for track in live:
            (done if index - track["pts"][-1][0] > 8 else still_live).append(track)
        live = still_live
    return [track for track in done + live if len(track["pts"]) >= 4]


def rim_visits(frames: list[Frame], hoop, tracks, fps: float) -> list[tuple[int, dict, int, int]]:
    """Every visit of a ball to the rim zone, visits closer than 1.5 s merged.

    Returns ``(anchor, track, first, last)`` frame indexes; the anchor is the
    frame where the ball is closest to the rim opening.
    """
    hits = []
    for index, frame in enumerate(frames):
        cx, top, w, _h = hoop[index]
        for d in frame:
            if d[0] == BALL and d[1] >= 0.2:
                x, y = _center(d)
                if abs(x - cx) < 1.0 * w and top - 0.8 * w < y < top + 0.6 * w:
                    hits.append((index, math.hypot(x - cx, y - top - 0.2 * w) / w))
                    break
    clusters: list[list[tuple[int, float]]] = []
    gap = int(round(1.5 * fps))
    for hit in hits:
        if clusters and hit[0] - clusters[-1][-1][0] <= gap:
            clusters[-1].append(hit)
        else:
            clusters.append([hit])
    visits = []
    for cluster in clusters:
        anchor = min(cluster, key=lambda item: item[1])[0]
        track = next(
            (item for item in tracks if any(point[0] == anchor for point in item["pts"])),
            {"pts": []},
        )
        visits.append((anchor, track, cluster[0][0], cluster[-1][0]))
    return visits


def visit_features(anchor: int, track: dict, hoop, frames: list[Frame], last: int, fps: float) -> dict[str, float]:
    """Trajectory and hoop-box features around one rim visit.

    Window lengths are defined at 30 fps and scaled to ``fps``.
    """
    def frames_for(count_at_30):
        return max(1, int(round(count_at_30 * fps / 30.0)))

    cx, top, w, _h = hoop[anchor]
    points = list(track["pts"])
    before = [p for p in points if anchor - frames_for(12) <= p[0] <= anchor]
    near = []
    for k in range(anchor + 1, min(len(frames), anchor + frames_for(31))):
        for d in frames[k]:
            if d[0] == BALL and d[1] >= 0.15:
                x, y = _center(d)
                if abs(x - cx) < 1.6 * w and top - 1.5 * w < y < top + 3 * w:
                    near.append((k, x, y, d[4] - d[2], d[1]))

    def velocity(ps):
        if len(ps) < 2:
            return 0.0, 0.0
        a, b = ps[0], ps[-1]
        dt = max(1, b[0] - a[0]) * 30.0 / fps
        return (b[1] - a[1]) / dt / w, (b[2] - a[2]) / dt / w

    vx_in, vy_in = velocity(before)
    f: dict[str, float] = {
        "vy_in": vy_in,
        "vx_in": abs(vx_in),
        "x_off_entry": min(
            [abs(p[1] - cx) / w for p in points if abs(p[0] - anchor) <= frames_for(2)],
            default=1.0,
        ),
    }
    occluded = 0
    for k in range(anchor, min(len(frames), anchor + frames_for(10))):
        if not any(
            d[0] == BALL and d[1] >= 0.15
            and abs(_center(d)[0] - cx) < 1.2 * w
            and top - w < _center(d)[1] < top + 1.5 * w
            for d in frames[k]
        ):
            occluded += 1
    f["occluded_frames"] = occluded * 30.0 / fps
    corridor = [p for p in near if abs(p[1] - cx) < 0.65 * w and top + 0.15 * w < p[2] < top + 1.6 * w]
    f["net_corridor_pts"] = len(corridor) * 30.0 / fps
    if len(corridor) >= 2:
        ordered = sorted(corridor)
        vy_net = (ordered[-1][2] - ordered[0][2]) / (max(1, ordered[-1][0] - ordered[0][0]) * 30.0 / fps) / w
    else:
        vy_net = vy_in
    f["vy_net"] = vy_net
    f["slowdown"] = vy_net / max(1e-3, vy_in) if vy_in > 0 else 1.0
    deep = [p for p in near if p[2] > top + 1.6 * w]
    f["exit_below"] = float(any(abs(p[1] - cx) < 1.0 * w for p in deep))
    f["exit_below_xoff"] = min([abs(p[1] - cx) / w for p in deep], default=3.0)
    f["rebound_up"] = float(any(p[2] < top - 0.5 * w for p in near if p[0] > anchor + frames_for(3)))
    f["lateral"] = max([abs(p[1] - cx) / w for p in near if p[2] < top + 0.8 * w], default=0.0)
    boxes = []
    for k in range(max(0, anchor - frames_for(15)), min(len(frames), last + frames_for(25))):
        hoops = [d for d in frames[k] if d[0] == HOOP]
        if hoops:
            d = max(hoops, key=lambda item: item[1])
            boxes.append(((d[3] + d[5]) / 2, d[5] - d[3], d[1]))
    if boxes:
        ys = [b[0] for b in boxes]
        heights = [b[1] for b in boxes]
        f["hoop_y_range"] = (max(ys) - min(ys)) / w
        f["hoop_h_range"] = (max(heights) - min(heights)) / w
        f["hoop_conf_min"] = min(b[2] for b in boxes)
    else:
        f["hoop_y_range"] = f["hoop_h_range"] = f["hoop_conf_min"] = 0.0
    return f


def net_motion_features(net_motion: Sequence[Sequence[float]], anchor: int, fps: float) -> dict[str, float]:
    """Pixel change inside the net and just below it, relative to the seconds before.

    ``net_motion[i]`` is ``(net, under)``: the share of pixels that changed
    since the previous frame in the net box and in the area under the net.
    """
    def frames_for(count_at_30):
        return max(1, int(round(count_at_30 * fps / 30.0)))

    base = net_motion[max(0, anchor - frames_for(45)):max(1, anchor - frames_for(10))]
    active = net_motion[anchor:anchor + frames_for(25)]
    if not active:
        return {"net_motion_rise": 0.0, "under_motion_rise": 0.0, "net_motion_frames": 0.0, "net_base": 0.0}
    base_net = median([row[0] for row in base]) if base else 0.0
    base_under = median([row[1] for row in base]) if base else 0.0
    return {
        "net_motion_rise": max(row[0] for row in active) - base_net,
        "under_motion_rise": max(row[1] for row in active) - base_under,
        "net_motion_frames": sum(1 for row in active if row[0] - base_net > 0.15) * 30.0 / fps,
        "net_base": base_net,
    }


def find_rim_events(
    frames: list[Frame],
    fps: float,
    time_offset: float = 0.0,
    net_motion: Sequence[Sequence[float]] | None = None,
) -> list[dict[str, Any]]:
    """All rim visits with features; times in seconds (plus ``time_offset``)."""
    frames, _static = drop_static_balls(frames)
    hoop = hoop_series(frames, fps)
    tracks = ball_tracks(frames, hoop)
    events = []
    for anchor, track, first, last in rim_visits(frames, hoop, tracks, fps):
        cx, top, w, h = hoop[anchor]
        events.append({
            "time": round(time_offset + anchor / fps, 3),
            "frame": anchor,
            "first_time": round(time_offset + first / fps, 3),
            "dur": round((last - first) / fps, 3),
            "hoop": {"center_x": cx, "top": top, "width": w, "height": h},
            "features": {
                **visit_features(anchor, track, hoop, frames, last, fps),
                **(net_motion_features(net_motion, anchor, fps) if net_motion is not None else {}),
                "dur": (last - first) / fps,
            },
        })
    return events


def load_make_model(path: Path | str = MODEL_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def make_probability(features: dict[str, float], model: dict) -> float:
    """Logistic make probability; features are clipped like during training."""
    z = model["bias"]
    for key, mean, scale, weight in zip(model["keys"], model["mean"], model["scale"], model["weights"]):
        value = min(float(features.get(key, 0.0)), model.get("clip", 20.0))
        z += weight * (value - mean) / scale
    return 1.0 / (1.0 + math.exp(-z))


def fit_make_model(rows: list[dict[str, float]], labels: list[int], keys: Sequence[str], l2: float = 1.0, steps: int = 3000, clip: float = 20.0) -> dict:
    """Plain L2 logistic regression (no extra dependencies)."""
    import numpy as np

    x = np.array([[min(float(row.get(k, 0.0)), clip) for k in keys] for row in rows])
    y = np.array(labels, dtype=float)
    mean = x.mean(0)
    scale = x.std(0) + 1e-6
    z = (x - mean) / scale
    weights = np.zeros(z.shape[1])
    bias = 0.0
    for _ in range(steps):
        p = 1 / (1 + np.exp(-(z @ weights + bias)))
        weights -= 0.5 * (z.T @ (p - y) / len(y) + l2 * weights / len(y))
        bias -= 0.5 * float((p - y).mean())
    return {
        "keys": list(keys),
        "mean": mean.round(6).tolist(),
        "scale": scale.round(6).tolist(),
        "weights": weights.round(6).tolist(),
        "bias": round(bias, 6),
        "clip": clip,
    }


MODEL_KEYS = (
    "hoop_h_range", "hoop_y_range", "hoop_conf_min", "net_corridor_pts",
    "occluded_frames", "exit_below", "exit_below_xoff", "rebound_up", "lateral",
    "vy_in", "vx_in", "x_off_entry", "slowdown", "vy_net", "dur",
    "net_motion_rise", "under_motion_rise", "net_motion_frames", "net_base",
)
