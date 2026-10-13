"""Unit tests for backend/at_models.py (synthetic fixtures, no I/O)."""
from backend import at_models as m


def _rising(n=30, base=25700.0, step=8.0):
    return [{"o": base + i * step - 2, "h": base + i * step + 3,
             "l": base + i * step - 3, "c": base + i * step, "v": 1000}
            for i in range(n)]


def _flat(n=30, px=25800.0):
    return [{"o": px, "h": px + 2, "l": px - 2, "c": px, "v": 1000} for _ in range(n)]


def _rows():
    return [
        {"strike": 25750, "callOI": 1000, "putOI": 1500, "callLTP": 150, "putLTP": 60},
        {"strike": 25800, "callOI": 1200, "putOI": 3000, "callLTP": 120, "putLTP": 100, "atm": True},
        {"strike": 25850, "callOI": 4000, "putOI": 1800, "callLTP": 95, "putLTP": 130},
    ]


def test_vwap_and_trend():
    bars = _rising()
    vw = m.session_vwap(bars)
    assert vw is not None and vw < bars[-1]["c"]  # rising → price above VWAP
    t = m.trend_score(bars, vw)
    assert t["score"] == 2, t
    assert m.session_vwap([{"c": 1, "v": 0}]) is None
    tf = m.trend_score(_flat(), 25800.0)
    assert tf["score"] == 0, tf  # inside tolerance → neutral


def test_trigger_and_volatility():
    bars = _rising()
    bars[-1]["c"] = bars[-1]["c"] + 40  # punch through prior highs
    bars[-1]["h"] = bars[-1]["c"] + 1
    from backend import autotrender_engine as e
    found = e.detect(bars)
    assert found["breakout"] == "HIGH"
    trig = m.trigger_ok(found, bars)
    assert trig == {"fire": True, "direction": "BULLISH", "reason": trig["reason"]}
    assert trig["reason"].startswith("breakout=HIGH")
    assert m.volatility_gate(bars)["ok"] is True
    chop = _flat()
    chop[-1]["h"] = 25800.2
    chop[-1]["l"] = 25799.8  # range 0.4 vs median 4 → chop veto
    assert m.volatility_gate(chop) == {
        "ok": False, "reason": m.volatility_gate(chop)["reason"]}
    assert m.volatility_gate(chop)["ok"] is False


def test_positioning_and_fuse():
    rows = _rows()  # total PCR = 6300/6200 ≈ 1.02 → neutral band
    p = m.positioning(rows, 25810.0, "BULLISH")
    assert p["score"] == 0 and p["veto"] is False
    # heavy call wall just above spot → veto for longs
    wall_rows = [dict(r) for r in rows]
    wall_rows[2] = dict(wall_rows[2], callOI=20000)
    pv = m.positioning(wall_rows, 25810.0, "BULLISH")
    assert pv["veto"] is True and pv["score"] in (-1, 0, 1)
    # full fire path
    fire = m.fuse({"score": 2}, {"fire": True, "direction": "BULLISH"},
                  {"score": 1, "veto": False}, {"ok": True})
    assert fire["fire"] is True and fire["score"] == 100.0
    assert fire["direction"] == "BULLISH" and fire["version"] == m.MODEL_VERSION
    # veto blocks even a perfect score
    blocked = m.fuse({"score": 2}, {"fire": True, "direction": "BULLISH"},
                     {"score": 1, "veto": True}, {"ok": True})
    assert blocked["fire"] is False
    # direction disagreement blocks
    disagree = m.fuse({"score": -2}, {"fire": True, "direction": "BULLISH"},
                      {"score": 1, "veto": False}, {"ok": True})
    assert disagree["fire"] is False
