"""Replay harness: walk a 1-min tape bar-by-bar through v1 (trigger+fuse)
and v2 (all tape models + fuse_v2), printing every v1 fire with its v2
verdict. Chain/flow/skew/straddle/vix pass neutral — tape-only comparison.

Usage: python3 tools/replay_day.py /tmp/nifty_full.json
"""
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from backend import at_models as m
from backend import autotrender_engine as e


def main(path: str) -> None:
    bars = json.load(open(path))["bars"]
    v1_fires = v2_fires = v2_vetoes = 0
    print(f"{'t':5} {'px':>9} v1-fire v2-fire v2-vetoes")
    for i in range(20, len(bars)):
        w = bars[: i + 1]
        t = datetime.fromtimestamp(w[-1]["t"], timezone.utc).strftime("%H:%M")
        vw = m.session_vwap(w)
        try:
            found = e.detect(w)
        except ValueError:
            continue
        trig = m.trigger_ok(found, w)
        trend, vol = m.trend_score(w, vw), m.volatility_gate(w)
        v1 = m.fuse(trend, trig, {"score": 0, "veto": False}, vol)
        v2 = m.fuse_v2(trend, trig, {"score": 0, "veto": False}, vol,
                       m.trend_strength(w), m.higher_tf_bias(w),
                       m.exhaustion(w, vw), m.extension_check(w),
                       m.htf_exhaustion(w))
        if v1["fire"]:
            v1_fires += 1
            mark = ""
            if v2["fire"]:
                v2_fires += 1
                mark = "AGREE"
            else:
                v2_vetoes += 1
                mark = f"VETO {v2['vetoes']}"
            print(f"{t} {w[-1]['c']:>9.1f} {v1['direction']:>7} "
                  f"{str(v2['fire']):>5} {mark} (v1={v1['score']} v2={v2['score']})")
    print(f"\ntape bars={len(bars)} v1_fires={v1_fires} "
          f"v2_agree={v2_fires} v2_vetoed={v2_vetoes}")


if __name__ == "__main__":
    main(sys.argv[1])
