"""Engine v2 tape-model tests: ADX, Supertrend, HTF bias, extension,
exhaustion, and fuse_v2 vetoes. v1 fuse() behavior is pinned by
test_at_models.py and must keep passing unchanged."""
from backend import at_models as m
from backend import autotrender_engine as e


def _bars(n, start=100.0, step=1.0, rng=1.0, vol=10):
    out = []
    px = start
    for _ in range(n):
        o = px
        px += step
        out.append({"o": o, "h": max(o, px) + rng / 2, "l": min(o, px) - rng / 2,
                    "c": px, "v": vol})
    return out


def _flat(n, px=100.0, rng=0.4):
    import math
    out = []
    for i in range(n):
        c = px + rng * math.sin(i / 4.0)  # oscillation, no cumulative drift
        out.append({"o": px, "h": px + rng, "l": px - rng, "c": c, "v": 10})
    return out


def test_adx_trends_up():
    d = e.adx(_bars(60, step=1.0))
    assert d["adx"] > 20, d
    assert d["plus_di"] > d["minus_di"], d


def test_adx_flat_is_weak():
    d = e.adx(_flat(60))
    assert d["adx"] < 20, d


def test_adx_needs_data():
    assert e.adx(_bars(5)) == {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}


def test_supertrend_follows_trend():
    assert e.supertrend(_bars(40, step=1.0))["direction"] == "UP"
    assert e.supertrend(_bars(40, step=-1.0))["direction"] == "DOWN"


def test_resample_groups():
    bars = _bars(30, start=10.0, step=1.0)
    tf = e.resample(bars, 15)
    assert len(tf) == 2
    assert tf[0]["o"] == 10.0
    assert tf[1]["c"] == bars[-1]["c"]
    assert tf[0]["h"] >= tf[0]["l"]
    assert tf[0]["v"] == 150


def test_trend_strength_regimes():
    assert m.trend_strength(_bars(60, step=1.0))["regime"] == "TREND"
    assert m.trend_strength(_flat(60))["regime"] == "RANGE"


def test_htf_bias_tracks_direction():
    assert m.higher_tf_bias(_bars(300, step=0.2))["bias"] == "BULL"
    assert m.higher_tf_bias(_bars(300, step=-0.2))["bias"] == "BEAR"


def test_extension_vetoes_spent_move():
    bars = _bars(10, start=100.0, step=10.0, rng=1.0)  # day range ~100
    bars += _bars(20, start=bars[-1]["c"], step=0.1, rng=1.0)
    ext = m.extension_check(bars)
    assert ext["vetoed"] and ext["ratio"] > 8, ext


def test_extension_passes_normal_day():
    assert not m.extension_check(_bars(60, step=0.1))["vetoed"]


def test_exhaustion_flags_climax_into_extreme():
    bars = _bars(30, step=0.5) + _bars(10, start=100.0 + 30 * 0.5, step=3.0)
    x = m.exhaustion(bars, vwap=bars[0]["o"])
    assert x["stretched"] and x["z"] > 3 and x["climax"] > 1.3, x


def test_exhaustion_passes_steady_trend_ride():
    # Straight drift sits ATRs from VWAP but never accelerates: a ride,
    # not a climax — must NOT veto or trend days go silent.
    bars = _bars(60, step=0.5)
    x = m.exhaustion(bars, vwap=bars[0]["o"])
    assert x["z"] > 3 and not x["stretched"], x


def test_exhaustion_ignores_chop_denom_noise():
    # Sideways chop: 20-bar drift ~0 must not print climax >100 and veto
    # the next valid range-break (live bug 2026-10-14: climax=106.77).
    bars = _flat(60, px=22620.0, rng=3.0)
    x = m.exhaustion(bars, vwap=22620.0)
    assert not x["stretched"] and x["climax"] == 0.0, x


def test_exhaustion_calm_near_vwap():
    bars = _flat(40)
    vwap = sum(b["c"] for b in bars) / len(bars)
    assert not m.exhaustion(bars, vwap=vwap)["stretched"]


def _fuse_args(direction="BULLISH", **kw):
    base = dict(
        trend={"score": 2}, trigger={"fire": True, "direction": direction},
        pos={"score": 1, "veto": False}, vol={"ok": True},
        strength={"trending": True}, htf={"bias": "BULL" if direction == "BULLISH" else "BEAR"},
        exh={"z": 0.5, "stretched": False, "side": 0},
        ext={"vetoed": False, "ratio": 3.0})
    base.update(kw)
    return base


def test_fuse_v2_fires_on_full_agreement():
    f = m.fuse_v2(**_fuse_args())
    assert f["fire"] and f["direction"] == "BULLISH" and f["vetoes"] == [], f


def test_fuse_v2_htf_disagree_vetoes():
    f = m.fuse_v2(**_fuse_args(htf={"bias": "BEAR"}))
    assert not f["fire"] and "htf-disagree" in f["vetoes"], f


def test_fuse_v2_no_trend_vetoes_chop_breakout():
    f = m.fuse_v2(**_fuse_args(strength={"trending": False}))
    assert not f["fire"] and "no-trend" in f["vetoes"], f


def test_fuse_v2_range_break_exempt_from_no_trend():
    # A fresh Donchian break is a trend being born: ADX may be young.
    args = _fuse_args(strength={"trending": False})
    args["trigger"] = {"fire": True, "direction": "BULLISH",
                       "reason": "breakout=HIGH cross_up rsi=64.0 vol_spike=False"}
    f = m.fuse_v2(**args)
    assert f["fire"] and "no-trend" not in f["vetoes"], f


def test_fuse_v2_exhaust_chase_vetoes():
    f = m.fuse_v2(**_fuse_args(direction="BEARISH",
                                exh={"z": -2.8, "stretched": True, "side": -1}))
    assert not f["fire"] and "exhaust-chase" in f["vetoes"], f


def test_fuse_v2_extended_move_vetoes():
    f = m.fuse_v2(**_fuse_args(ext={"vetoed": True, "ratio": 11.2}))
    assert not f["fire"] and "extended" in f["vetoes"], f


def test_htf_exhaustion_abstains_when_young():
    h = m.htf_exhaustion(_bars(100, step=0.2))
    assert h["rsi"] is None and not h["veto_bear"] and not h["veto_bull"], h


def test_htf_exhaustion_flags_oversold():
    bars = _bars(300, step=-0.3)
    h = m.htf_exhaustion(bars)
    assert h["extreme"] == "OVERSOLD" and h["veto_bear"], h


def test_fuse_v2_htf_oversold_vetoes_bearish():
    f = m.fuse_v2(**_fuse_args(direction="BEARISH",
                               htfx={"rsi": 24.0, "veto_bear": True,
                                     "veto_bull": False}))
    assert not f["fire"] and "htf-oversold" in f["vetoes"], f


def test_fuse_v2_supertrend_disagree_vetoes_abstain_passes():
    f = m.fuse_v2(**_fuse_args(stw={"direction": "DOWN"}))
    assert not f["fire"] and "st-disagree" in f["vetoes"], f
    f = m.fuse_v2(**_fuse_args(stw={"direction": "UP"}))
    assert f["fire"], f
    f = m.fuse_v2(**_fuse_args(stw={"direction": None, "value": None}))
    assert f["fire"], f


def test_composite_positioning_aligns_skew_and_clamps():
    pos = {"score": 1, "veto": False}
    c = m.composite_positioning(pos, {"score": 1}, {"score": -1}, "BULLISH")
    assert c["score"] == 1 and not c["veto"], c  # 1+1-1 = 1 (skew fear fights bulls)
    c = m.composite_positioning({"score": 0, "veto": False},
                                {"score": -1}, {"score": 1}, "BULLISH")
    assert c["score"] == 0, c  # 0-1+1 = 0 (call-side greed offsets flow)
    c = m.composite_positioning({"score": 0, "veto": True},
                                {"score": 1}, {"score": 0}, "BEARISH")
    assert c["veto"] and c["score"] == 1, c


def test_fuse_v2_v1_weights_still_fire_solo():
    # v1 fuse untouched: same inputs must still fire under v1 rules.
    from backend.at_models import fuse
    f = fuse({"score": 2}, {"fire": True, "direction": "BULLISH"},
             {"score": 1, "veto": False}, {"ok": True})
    assert f["fire"], f


def _chain_row(strike, c_oi, c_ltp, p_oi, p_ltp, atm=False):
    return {"strike": strike, "callOI": c_oi, "callLTP": c_ltp,
            "putOI": p_oi, "putLTP": p_ltp, "atm": atm}


def test_oi_flow_supports_bullish_buildup():
    prev = [_chain_row(22600, 1000, 50.0, 1000, 60.0, atm=True),
            _chain_row(22650, 800, 30.0, 900, 80.0),
            _chain_row(22550, 900, 70.0, 800, 40.0)]
    cur = [_chain_row(22600, 1500, 55.0, 800, 55.0, atm=True),
           _chain_row(22650, 1200, 34.0, 700, 75.0),
           _chain_row(22550, 1300, 75.0, 600, 36.0)]
    assert m.oi_flow(prev, cur, 22600, "BULLISH")["score"] == 1
    assert m.oi_flow(prev, cur, 22600, "BEARISH")["score"] == -1


def test_oi_flow_abstains_without_direction_or_strikes():
    rows = [_chain_row(22600, 1, 1.0, 1, 1.0, atm=True)]
    assert m.oi_flow(rows, rows, 22600, None)["score"] == 0
    assert m.oi_flow(rows, rows, 22600, "BULLISH")["score"] == 0


def test_iv_skew_symmetric_is_flat():
    # Put-call parity at S=K: C - P ≈ S(1-e^-rT) ≈ 20.1 → same IV both sides.
    rows = [_chain_row(22600, 1000, 160.0, 1000, 140.0, atm=True)]
    s = m.iv_skew(rows, 22600, 5.0)
    assert s["score"] == 0 and s["skew"] is not None and abs(s["skew"]) < 0.02, s


def test_iv_skew_fear_bid_is_bearish_context():
    rows = [_chain_row(22600, 1000, 120.0, 1000, 185.0, atm=True)]
    s = m.iv_skew(rows, 22600, 5.0)
    assert s["score"] == -1 and s["skew"] > 0.02, s


def test_straddle_momentum_direction():
    prev = [_chain_row(22600, 1, 150.0, 1, 150.0, atm=True)]
    up = [_chain_row(22600, 1, 160.0, 1, 160.0, atm=True)]
    dn = [_chain_row(22600, 1, 140.0, 1, 140.0, atm=True)]
    assert m.straddle_momentum(prev, up)["expanding"] is True
    assert m.straddle_momentum(prev, dn)["expanding"] is False


def test_vix_regime_panic_vetoes_calm_passes():
    assert m.vix_regime(22.0, 20.0)["veto"] is True
    assert m.vix_regime(22.0, 23.0)["veto"] is False
    assert m.vix_regime(12.0)["state"] == "calm"
    assert m.vix_regime(None)["state"] == "unknown"


def test_fuse_v2_vix_panic_and_crush_veto():
    f = m.fuse_v2(**_fuse_args(vixr={"veto": True, "state": "panic"}))
    assert not f["fire"] and "vix-panic" in f["vetoes"], f
    f = m.fuse_v2(**_fuse_args(strad={"expanding": False, "chg_pct": -7.5}))
    assert not f["fire"] and "straddle-crush" in f["vetoes"], f
