"""Retrain the make model from labelled rim events and report leave-one-video-out results.

Dataset JSON: ``{"<video>": [{"features": {...}, "label": 1 | 0 | "unk" | "unlab"}, ...]}``.
Only events labelled 1 (make) or 0 (not a make) are used for training.

    python scripts/train_make_model.py data/labels/rim_events_20261021.json --write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from basketball_highlight.rim_events import MODEL_KEYS, MODEL_PATH, fit_make_model, make_probability  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", nargs="+")
    parser.add_argument("--threshold", type=float, default=0.35)
    parser.add_argument("--write", action="store_true", help=f"overwrite {MODEL_PATH.name}")
    args = parser.parse_args()
    data = {}
    for path in args.dataset:
        data.update(json.loads(Path(path).read_text(encoding="utf-8")))
    labelled = {video: [e for e in events if e.get("label") in (0, 1)] for video, events in data.items()}
    found = missed = false = 0
    for video in data:
        train = [e for other, events in labelled.items() if other != video for e in events]
        model = fit_make_model([e["features"] for e in train], [e["label"] for e in train], MODEL_KEYS)
        for event in labelled[video]:
            hit = make_probability(event["features"], model) >= args.threshold
            found += event["label"] == 1 and hit
            missed += event["label"] == 1 and not hit
            false += event["label"] == 0 and hit
    print(f"leave-one-video-out @ {args.threshold}: makes {found}/{found + missed}, false {false}")
    if args.write:
        events = [e for items in labelled.values() for e in items]
        model = fit_make_model([e["features"] for e in events], [e["label"] for e in events], MODEL_KEYS)
        model["trained_on"] = (
            f"{len(data)} videos, {sum(e['label'] == 1 for e in events)} makes / "
            f"{sum(e['label'] == 0 for e in events)} non-makes"
        )
        MODEL_PATH.write_text(json.dumps(model, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {MODEL_PATH}")


if __name__ == "__main__":
    main()
