# Autotrender Desk — living state (updated 2026-10-16 ~00:30 IST)

## Mission
Personal Nifty-options auto-signal engine → validate 1 month on paper, then
calibrate per-group profiles from real paper tape. Paper-only. No real orders.

## Engine (backend/autotrender.py + at_models.py + day_regime.py)
- 19 books, evaluated every 60s: NIFTY, SENSEX (bse_fo), CRUDEOIL, NATGASMINI
  (fut NATURALGAS), GOLDM (fut GOLD), + 14 equity (EQ tape, nse_fo chain).
- Fire bar: score 70 + 2/5 witnesses + regime gate + fusion vetoes. No cap.
- Regime: EARLY → TREND-DAY-CANDIDATE / CHOP, lock 45 bars, recheck 15 min,
  scratch 0.15%/30 bars, 2 scratches → silent. Re-entry: reclaim+hold 3.
- Shadow book: vetoed setups ≥55 logged, judged +30 bars (≥0.5% = win).
- Profiles: index (NIFTY) / sensex / equity / commodity. Same seeds now;
  paper tape diverges them. Manual tuning; auto-tuner later (shadow harness).
- Strategy scopes (exits/sizing, trade_manager + strategy.json): default /
  NIFTY / SENSEX / EQUITY / COMMODITY. VM-live default+NIFTY values preserved;
  new scopes backfilled from default. Lots branch unchanged (NIFTY scope lots,
  else global lot_size).
- Duplicates fixed 2026-10-15: _ingest forwards OR mirrors, never both.

## Universe (verified 2026-10-15, true-ATM notional)
- Stocks: HDFCBANK RELIANCE BHARTIARTL SBIN KOTAKBANK BSE ICICIBANK INFY
  AXISBANK TITAN TCS ITC LT M&M. Excluded: CANBK (thin, ₹2.40 prem),
  ULTRACEMCO/SUNPHARMA/BAJFINANCE (thin), IDEA/YESBANK/SUZLON (penny),
  TATAMOTORS (demerged → TMPV).
- Commodities: natgas std+mini alive, GOLDM alive, crude std-only (mini dead).

## Data & calibration plan
- Paper + shadows accumulate daily. 30 closed trades/group before touching
  knobs; walk-forward; ≤5%/knob/night; audit. Equity ~2 wks, rest ~6-8 wks.
- ticks.db 656MB is stale ballast (SAVE_TICKS=0). Re-enable bounded for tuner.

## Deploy
- scripts/deploy.sh ONLY (tests gate push+restart). origin/main ↔ VM
  /home/ubuntu/kotaktegramv2 (pm2 kotak-trader). strategy.json is VM-local
  live tuning — never overwrite.

## Open / verify 2026-10-16 09:15
1. FUT subs streaming (CRUDEOIL/NATURALGAS/GOLD confirmed resolving).
2. NATGASMINI/GOLDM books warming; crude Nov chain quotes back.
3. First call prints ONCE in messages. 4. Equity live spreads re-pull.

## Engine changes 2026-10-16 night (204d515, live)
- Removed fire cap (was dead code) + scratch→silent rule (b2e0390).
- NEW location witness: backend/day_location.py — IB (first 60 bars) +
  developing POC/VAH/VAL (70% volume profile); 6th fusion witness, confirms
  only (abstains on thin tape <65 bars). Ballot shows Location row.
- Signal expiry: scratched-fire reentry expires after 30 bars
  (REENTRY_SIGNAL_BARS); breakout cooldown-jump is episode-based — jumps
  only if no breakout fired in last 30 min (BREAKOUT_EPISODE_SECS).
- First live test Monday 2026-10-19 (NIFTY weekly expiry): watch call
  frequency (governors off) + Location row on ballot.

## Stability fix 2026-10-23 (dbf9da6) — restart storm
Diagnosis: watchdog restarted the process ~159×/day (859 total) because
/api/status could not answer within 10s. Root: synchronous Kotak REST
(chain_snapshot, 30s SDK rate-limiter wait + 30s HTTP timeout) ran inline
in async scheduler_tick; a 429 storm (option-chain quota) blocked the
event loop for minutes every sweep.
- chain_snapshot now runs via asyncio.to_thread (scheduler + /chain-probe).
- kotak_chain: 120s snapshot cache (CHAIN_TTL_SECS), per-day expiry cache,
  429 → exponential cooldown 60→300s serving stale snapshots, no quotes
  fallback while throttled; state visible at /api/health.
- Contract masters: same-day marker (data/sensex_contracts.csv.date) skips
  the 4-CSV re-download on restarts; daily 08:50 refresh forces.
- Watchdog: probes /api/health, needs 2 consecutive misses, max 4 restarts
  per hour, state in data/watchdog-{fails,restarts}. Repo copy:
  scripts/watchdog.sh; VM copy installed at repo root (backup .bak-*).
- Verify next session: pm2 restart counter flat, data/watchdog.log clean,
  /api/health option_chain.hits only rises briefly on throttle.
