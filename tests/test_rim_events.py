from basketball_highlight.rim_events import (
    MODEL_KEYS,
    drop_static_balls,
    find_rim_events,
    fit_make_model,
    load_make_model,
    make_probability,
    net_motion_features,
)

HOOP = [1, 0.9, 230, 290, 290, 330]  # center x 260, top 290, width 60


def _frames(ball_path, stretch_from=None, total=90):
    frames = []
    for index in range(total):
        frame = [list(HOOP)]
        if stretch_from is not None and stretch_from <= index < stretch_from + 10:
            frame[0] = [1, 0.5, 230, 300, 290, 370]  # net pushed down by the ball
        if index in ball_path:
            x, y = ball_path[index]
            frame.append([0, 0.8, x - 14, y - 14, x + 14, y + 14])
        frames.append(frame)
    return frames


def test_ball_dropping_through_rim_is_one_event_with_stretched_hoop_box():
    path = {i: (260, 150 + 10 * (i - 20)) for i in range(20, 45)}
    events = find_rim_events(_frames(path, stretch_from=34), fps=30.0)

    assert len(events) == 1
    assert abs(events[0]["time"] - 35 / 30) < 0.05
    assert events[0]["features"]["hoop_h_range"] >= 0.5


def test_ball_far_from_rim_makes_no_event():
    path = {i: (60, 150 + 10 * (i - 20)) for i in range(20, 45)}
    assert find_rim_events(_frames(path), fps=30.0) == []


def test_static_ball_detection_is_dropped():
    frames = [[list(HOOP), [0, 0.5, 270, 250, 300, 280]] for _ in range(200)]
    cleaned, static = drop_static_balls(frames)
    assert static
    assert all(d[0] == 1 for frame in cleaned for d in frame)


def test_net_motion_features_measure_rise_over_baseline():
    motion = [[0.02, 0.01]] * 60 + [[0.5, 0.3]] * 5 + [[0.02, 0.01]] * 30
    features = net_motion_features(motion, 60, 30.0)
    assert features["net_motion_rise"] > 0.4
    assert features["net_motion_frames"] == 5


def test_fit_and_bundled_model_score_in_range():
    rows = [{key: 1.0 for key in MODEL_KEYS}, {key: 0.0 for key in MODEL_KEYS}] * 5
    model = fit_make_model(rows, [1, 0] * 5, MODEL_KEYS, steps=200)
    assert make_probability(rows[0], model) > make_probability(rows[1], model)
    bundled = load_make_model()
    assert bundled["keys"] == list(MODEL_KEYS)
    assert 0.0 <= make_probability({}, bundled) <= 1.0
