# Autotrender deploy bundle (VM: /home/ubuntu/telegram-kotak-trader)

Scope: everything is ADDITIVE. No existing route, strategy, or fill logic
was modified. Untracked `frontend/autotrender.js` is orphaned (panel
removed) — safe to delete on the VM or leave; it is not loaded.

## Files (from this repo copy)

Modified (29 insertions, 0 deletions):

- `backend/main.py` — autotrender router include + scheduler start/stop in lifespan
- `frontend/index.html` — 📊 header button (opens autotrender page)

New:

- `backend/autotrender.py` — inbound `/call` API, message mirror,
  guarded auto-forward, scheduler loop
- `backend/autotrender_engine.py` — pure indicators (JS-parity tested)
- `backend/candle_builder.py` — ticks → 1-min bars
- `backend/kotak_chain.py` — Kotak quotes → chain rows for the engine
- `tests/test_autotrender_*.py`, `tests/test_candle_builder.py`,
  `tests/test_kotak_chain.py` — 20 tests

## Deploy (I can't reach your VM — run these yourself)

```bash
cd /home/ubuntu/telegram-kotak-trader
git stash list   # confirm clean-ish; commit or stash local diffs first
# copy the 2 modified + 4 new backend files + index.html from this machine
# (scp example, run from your Mac):
# scp backend/autotrender.py backend/autotrender_engine.py \
#     backend/candle_builder.py backend/kotak_chain.py \
#     backend/main.py frontend/index.html \
#     ubuntu@<VM-IP>:/home/ubuntu/telegram-kotak-trader/<same paths>
# on the VM:
python3 -m pytest tests/test_autotrender_engine.py tests/test_autotrender_scheduler.py \
  tests/test_candle_builder.py tests/test_kotak_chain.py -q
pm2 restart kotak-trader
pm2 logs kotak-trader --lines 50 --nostream
```

## Calibration (one look, first trading morning)

1. `GET /api/autotrender/engine-status` — after ~30 min of feed,
   NIFTY shows `bars`, `bias`, `rsi`. Before that: `warming up`.
2. First scheduler run logs `quote_keys` — confirm `open_interest` and
   `last_traded_price` present. If OI parses as 0 for every strike,
   send me the logged key list and I extend the parser (no redeploy
   of strategy code needed).
3. Scheduler marks: paper mode only, IST 09:15–15:30, 15-min cooldown
   per bias, max 20 auto calls/day. Tune in `backend/autotrender.py`
   (`SCHED_*` constants at the bottom).

## Rollback (60 seconds)

```bash
cd /home/ubuntu/telegram-kotak-trader
# disable scheduler only: set SCHED_MAX_PER_DAY = 0 in backend/autotrender.py
# full removal: git stash / git checkout -- backend/main.py frontend/index.html
#   + rm backend/autotrender*.py backend/candle_builder.py backend/kotak_chain.py
pm2 restart kotak-trader
```

## What to watch (first week)

- 📨 Messages with `sender=Autotrender` + matching paper trades
  (`source=autotrender-engine` vs `autotrender-personal`).
- Paper P&L per Autotrender trade — this is the edge proof, not backtests.
- Kill switch / stop-trading / mode switch all apply to engine calls
  unchanged (same `process_message` path as Telegram signals).
