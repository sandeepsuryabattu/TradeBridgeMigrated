"""Tests for performance analytics (pure compute + allowlist)."""
from backend import database as db


def _row(pnl, day="2026-10-14", sender="Target group", model=None, qty=0, entry=0):
    return {"pnl": pnl, "ist_date": day, "sender": sender, "model_version": model,
            "qty": qty, "entry": entry}


def test_compute_perf_overall_and_drawdown():
    rows = [_row(100), _row(-50), _row(200), _row(-300)]
    out = db.compute_perf(rows)
    o = out["overall"]
    assert o["trades"] == 4 and o["wins"] == 2 and o["win_rate"] == 50.0
    assert o["pnl"] == -50.0
    assert o["profit_factor"] == 0.86  # 300/350
    assert o["expectancy"] == -12.5
    assert out["max_drawdown"] == -50.0  # single day at -50 from peak 0
    eq = [p["equity"] for p in out["equity_curve"]]
    assert eq == [-50.0]
    assert o["deployed"] == 0.0  # no qty/entry on these rows


def test_deployed_sums_qty_times_entry():
    rows = [_row(100, qty=100, entry=10.0),
            _row(-50, day="2026-10-15", sender="Autotrender", qty=50, entry=20.0),
            _row(200, day="2026-10-15", sender="Autotrender", qty=25, entry=4.0)]
    out = db.compute_perf(rows)
    assert out["overall"]["deployed"] == 2100.0  # 1000 + 1000 + 100
    assert out["by_day"]["2026-10-14"]["deployed"] == 1000.0
    assert out["by_day"]["2026-10-15"]["deployed"] == 1100.0
    assert out["by_source"]["Autotrender"]["deployed"] == 1100.0
    # JSON-safe
    import json
    json.dumps(out)


def test_grouping_and_empty():
    rows = [_row(100, day="2026-10-13", sender="A", model="at-models-v1"),
            _row(-50, day="2026-10-14", sender="Autotrender", model="at-models-v1"),
            _row(200, day="2026-10-14", sender="Autotrender")]
    out = db.compute_perf(rows)
    assert set(out["by_day"]) == {"2026-10-13", "2026-10-14"}
    assert out["by_source"]["Autotrender"]["pnl"] == 150.0
    assert out["by_model"] == {"at-models-v1": out["by_model"]["at-models-v1"]}
    assert out["by_model"]["at-models-v1"]["trades"] == 2
    empty = db.compute_perf([])
    assert empty["overall"]["trades"] == 0 and empty["max_drawdown"] == 0.0


def test_model_fields_allowed():
    assert {"model_version", "model_score"} <= db._ALLOWED_SIGNAL_FIELDS


def test_all_wins_serializable():
    import json
    out = db.compute_perf([{"pnl": 100, "ist_date": "2026-10-14",
                            "sender": "A", "model_version": None}])
    assert out["overall"]["profit_factor"] is None  # no inf — JSON-safe
    json.dumps(out)
