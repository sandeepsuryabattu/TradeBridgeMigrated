"""Regime validation: per-day 1-min tapes from ticks.db -> morning classify
-> walk the day with v1+v2 fires -> 30-min excursion per fire.

Usage (on VM, where ticks.db lives):
  python3 tools/validate_regime.py /home/ubuntu/kotaktegramv2/data/ticks.db SENSEX

Counterfactuals printed at the end:
  A) actual fires (all days), B) trend-days only, C) chop-days reclaim-only.
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
from backend import at_models as m
from backend import autotrender_engine as e
from backend import day_regime as dr

IST = timezone(timedelta(hours=5, minutes=30))
EXCURSION_MIN = 30


def load_days(db: str, symbol: str):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT ltp, timestamp FROM ticks WHERE symbol=? ORDER BY timestamp",
        (symbol,)).fetchall()
    days = defaultdict(list)
    for ltp, ts in rows:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        day = dt.astimezone(IST).strftime("%Y-%m-%d")
        days[day].append((dt, float(ltp)))
    return days


def to_minute_bars(ticks):
    buckets = {}
    for dt, px in ticks:
        key = dt.astimezone(IST).replace(second=0, microsecond=0)
        b = buckets.setdefault(key, {"o": px, "h": px, "l": px, "c": px, "v": 0})
        b["h"] = max(b["h"], px)
        b["l"] = min(b["l"], px)
        b["c"] = px
        b["v"] += 1
    out = []
    prev_c = None
    for key in sorted(buckets):
        b = buckets[key]
        # corrupt-tick guard (same spirit as the live tick filter): a 1-min
        # bar gapping >0.5% from the prior close is a bad print, not a move.
        if prev_c and abs(b["c"] - prev_c) / prev_c > 0.005:
            continue
        out.append({"t": int(key.timestamp()), **b})
        prev_c = b["c"]
    return out


def excursion(bars, i, direction):
    """Max favorable / adverse CLOSE move (index pts) over next 30 bars.

    Closes only: intrabar h/l spikes from bad prints must not score the
    strategy. Returns (fav, adv) — adv is negative-or-zero by construction.
    """
    entry = bars[i]["c"]
    window = bars[i + 1: i + 1 + EXCURSION_MIN]
    if not window:
        return 0.0, 0.0
    moves = ([b["c"] - entry for b in window] if direction == "BULLISH"
             else [entry - b["c"] for b in window])
    return max(moves), min(moves)


def main(db: str, symbol: str) -> None:
    days = load_days(db, symbol)
    per_regime = defaultdict(list)   # label -> [(fav, adv)]
    per_regime_v1 = defaultdict(list)  # v1 fires: bigger sample, same question
    reclaim_chop = []                # chop-day reclaim setups -> excursion
    mornings = {}
    for day in sorted(days):
        bars = to_minute_bars(days[day])
        if len(bars) < 60:
            continue
        morning = dr.classify(bars[:45])
        label = morning["label"]
        mornings[day] = (label, len(bars))
        for i in range(45, len(bars)):
            w = bars[: i + 1]
            try:
                found = e.detect(w)
            except ValueError:
                continue
            vw = m.session_vwap(w)
            trig = m.trigger_ok(found, w)
            if not trig["fire"]:
                continue
            trend, vol = m.trend_score(w, vw), m.volatility_gate(w)
            v1 = m.fuse(trend, trig, {"score": 0, "veto": False}, vol)
            if v1["fire"]:
                fav1, adv1 = excursion(bars, i, trig["direction"])
                per_regime_v1[label].append((fav1, adv1))
            v2 = m.fuse_v2(trend, trig, {"score": 0, "veto": False}, vol,
                           m.trend_strength(w), m.higher_tf_bias(w),
                           m.exhaustion(w, vw), m.extension_check(w),
                           m.htf_exhaustion(w))
            if not v2["fire"]:
                continue
            fav, adv = excursion(bars, i, trig["direction"])
            per_regime[label].append((fav, adv))
            if label == "CHOP":
                rc = dr.reclaim(w)
                if rc["setup"] and rc["side"] == trig["direction"]:
                    reclaim_chop.append((fav, adv))

    print(f"{'day':12} {'regime':20} bars")
    for day in sorted(mornings):
        print(f"{day} {mornings[day][0]:20} {mornings[day][1]}")

    def stats(rows):
        if not rows:
            return "n=0"
        favs = [f for f, _ in rows]
        advs = [a for _, a in rows]
        good = sum(1 for f in favs if f >= 15.0)
        bad = sum(1 for a in advs if a <= -15.0)
        return (f"n={len(rows)} fav_mean={sum(favs)/len(favs):+.1f} "
                f"adv_mean={sum(advs)/len(advs):+.1f} "
                f"good={good} bad={bad}")

    print("\n--- v1 fires by morning regime (bigger sample, same question) ---")
    for label in ("TREND-DAY-CANDIDATE", "CHOP", "UNDECIDED"):
        print(f"{label:22} {stats(per_regime_v1[label])}")
    print("\n--- v2 fires by morning regime (pts, 30-min window) ---")
    for label in ("TREND-DAY-CANDIDATE", "CHOP", "UNDECIDED"):
        print(f"{label:22} {stats(per_regime[label])}")
    print(f"\nchop-day reclaim-only counterfactual: {stats(reclaim_chop)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "SENSEX")
