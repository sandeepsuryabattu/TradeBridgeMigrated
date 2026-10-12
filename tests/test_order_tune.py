"""Tests for per-trade tuning (order_tune.py)."""
import json


def _strategy():
    return {
        "lots": 5, "bouncePoints": 4, "activationPoints": 6.0,
        "activationSLOffset": 2.0, "trailGap": 4.0,
        "entryTimerMins": 10, "exitTimerMins": 60,
        "signalTrailInitialSL": "points_from_ltp",
        "signalTrailInitialSLPoints": 8.0,
        "slMode": "signal_trail",
        "snapLevels": [{"snapPts": 11, "offset": 4}, {"snapPts": 15, "offset": 5},
                       {"snapPts": 20, "offset": 5}],
        "snapTrailAfterL3": True, "snapTrailGap": 5.0,
    }


def test_build_tune_snapshot():
    from backend.order_tune import build_tune
    t = build_tune(_strategy(), 5, 100)
    assert t["lots"] == 5 and t["lot_multiplier"] == 100
    assert t["bounce_points"] == 4 and t["trail_gap"] == 4.0
    assert t["sl_mode"] == "signal_trail"
    assert t["snap_levels"][0] == {"snapPts": 11, "offset": 4}


def test_validate_patch_ok_and_roundtrip():
    from backend.order_tune import validate_patch, tune_to_json, parse_tune_json
    clean, errors = validate_patch({"lots": 2, "trailGap": 3.5,
                                    "snapLevels": [{"snapPts": 8, "offset": 1},
                                                   {"snapPts": 9, "offset": 1},
                                                   {"snapPts": 9, "offset": 1}]})
    assert errors == []
    assert clean == {"lots": 2, "trailGap": 3.5, "snapLevels": [
        {"snapPts": 8, "offset": 1}, {"snapPts": 9, "offset": 1}, {"snapPts": 9, "offset": 1}]}
    blob = tune_to_json({"trail_gap": 3.5, "bounce_points": 4})
    parsed = parse_tune_json(blob)
    assert parsed["trail_gap"] == 3.5 and parsed["bounce_points"] == 4
    assert parse_tune_json("garbage{{") is None
    assert parse_tune_json(None) is None


def test_validate_patch_errors():
    from backend.order_tune import validate_patch
    _, e1 = validate_patch({"lots": 0})
    assert any("Lots" in e for e in e1)
    _, e2 = validate_patch({"slMode": "moon"})
    assert e2
    _, e3 = validate_patch({"snapLevels": [{"snapPts": 1}]})
    assert e3
    _, e4 = validate_patch({})
    assert e4 == ["Nothing to update"]


def test_apply_patch_recomputes_quantity():
    from backend.order_tune import build_tune, apply_patch
    order = {"trade_id": 1, "status": "pending", "quantity": 500,
             "entry_low": 180.0, "entry_high": 190.0, "price": 190.0,
             "notes": "Paper BUY X @ 180.0-190.0 [bounce-back]",
             **build_tune(_strategy(), 5, 100)}
    tune = apply_patch(order, {"lots": 2, "bouncePoints": 7})
    assert order["quantity"] == 200
    assert order["bounce_points"] == 7
    assert tune["lots"] == 2


def test_apply_patch_entry_range():
    from backend.order_tune import build_tune, apply_patch
    order = {"trade_id": 1, "status": "pending", "quantity": 500,
             "entry_low": 180.0, "entry_high": 190.0, "price": 190.0,
             "notes": "Paper BUY X @ 180.0-190.0 [bounce-back]",
             **build_tune(_strategy(), 5, 100)}
    apply_patch(order, {"entryLow": 170.0, "entryHigh": 185.0})
    assert order["entry_low"] == 170.0 and order["entry_high"] == 185.0
    assert order["price"] == 185.0
    assert "@ 170.0-185.0" in order["notes"]
    # Zero-width widens like placement
    apply_patch(order, {"entryLow": 150.0, "entryHigh": 150.0})
    assert order["entry_low"] == 148.0 and order["entry_high"] == 152.0
    # Incoherent range rejected
    import pytest
    with pytest.raises(ValueError):
        apply_patch(order, {"entryLow": 200.0, "entryHigh": 100.0})


def test_tune_from_order_legacy_fallback():
    from backend.order_tune import tune_from_order
    t = tune_from_order({"quantity": 300})  # pre-tune rows: no lots/multiplier
    assert t["lot_multiplier"] == 1 and t["lots"] == 300
    assert t["sl_mode"] == "signal_trail"
