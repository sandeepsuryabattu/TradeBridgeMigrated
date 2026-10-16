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
