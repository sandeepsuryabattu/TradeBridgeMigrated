"""
Kotak chain snapshots — live option-chain rows for the autotrender engine.

Primary source: Kotak Neo native `option_chain` REST endpoint, which returns
per-strike quote + openInterest{current, previous, change, changePct} —
server-computed change-in-OI. Fallback: `quotes` REST API over NSE_FO option
tokens from the contract master (carries OI but NO ChgOI; rows then carry
callChgOI/putChgOI = 0 and PCR signals must use total-OI PCR, never
calc_pcr_chg on zeros).

Honest limits (verified against installed kotakneoapi 3.0.7 + official docs):
- `quotes` has no ChgOI (fallback path only).
- No historical-candles use here: intraday candles come from candle_builder.
- Quote field names are normalised defensively: every snapshot records
  `quote_keys` (sorted raw keys) so the VM's first live response calibrates
  the parser — check logs, extend maps if a field lands at 0.

Nothing here trades. Output rows are engine-ready
(strike/callOI/putOI/callLTP/putLTP/atm) for autotrender_engine.
"""
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def _nearest_expiry_via_api(kotak_client: Any, segment: str, und: str) -> Optional[date]:
    """Nearest live expiry from the expiries API (clean ISO dates, no 2FA
    needed). The scrip-master expiry *fields* are stale (2016 timestamps),
    so this is the trustworthy source when the master can't deliver."""
    try:
        fn = getattr(kotak_client, "expiries", None)
        if fn is None:
            return None
        resp = fn(exchange=segment, underlying=und)
        dates = (resp or {}).get("expiries", []) if isinstance(resp, dict) else []
        today = date.today()
        parsed = []
        for s in dates:
            try:
                d = datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if d >= today:
                parsed.append(d)
        return min(parsed) if parsed else None
    except Exception:
        log.exception("chain_snapshot: expiries API failed")
        return None


def _first(d: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return default


def parse_quote(q: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise one Kotak quote dict. Never raises on odd shapes."""
    q = q or {}
    return {
        "ltp": _first(q, "last_traded_price", "ltp", "lp", "lastTradedPrice"),
        "oi": _first(q, "open_interest", "oi", "openInterest", "OI"),
        "o": _first(q, "open_price", "open", "day_open"),
        "h": _first(q, "high_price", "high", "day_high"),
        "l": _first(q, "low_price", "low", "day_low"),
        "c": _first(q, "close_price", "close", "previous_close", "prev_close"),
        "volume": _first(q, "volume_traded_today", "volume", "vol", "total_traded_quantity"),
        "quote_keys": sorted(str(k) for k in q.keys()),
    }


def spot_from_feed(market_feed: Any, symbol: str = "NIFTY") -> Optional[float]:
    """Latest LTP for an index symbol from live feed subscriptions."""
    try:
        subs = getattr(market_feed, "_subscriptions", {}) or {}
        want = (symbol or "").upper().strip()
        for info in subs.values():
            if str(info.get("symbol", "")).upper().strip() == want:
                ltp = float(info.get("ltp") or 0)
                if ltp > 0:
                    return ltp
    except Exception:
        log.exception("spot_from_feed failed")
    return None


def _token_of(contract: Dict[str, Any]) -> str:
    return str(contract.get("pSymbol", "") or contract.get("pInstrumentToken", "")
               or contract.get("instrument_token", ""))


def _snapshot_via_option_chain(kotak_client: Any, *, underlying: str,
                               segment: str, spot: Optional[float],
                               width: int) -> Dict[str, Any]:
    """Native option_chain endpoint — carries server-computed ChgOI."""
    oc = getattr(kotak_client, "option_chain", None)
    if oc is None:
        raise ValueError("option_chain API unavailable on client")
    und = (underlying or "").upper().strip()
    # count must be a multiple of 10; 20 strikes cover our ±9 window.
    resp = oc(exchange=segment, underlying=und,
              instrument_type="option", count=20)
    if isinstance(resp, dict) and ("error" in resp or "Error" in resp):
        raise ValueError(f"option_chain error: {resp}")
    data = resp.get("data", resp) if isinstance(resp, dict) else None
    if not isinstance(data, dict):
        raise ValueError("option_chain: empty/unexpected response")

    by_strike: Dict[int, Dict[str, Any]] = {}
    quote_keys: List[str] = []
    for side, items in (("CE", data.get("call", [])),
                        ("PE", data.get("put", []))):
        for leg in items or []:
            leg = leg or {}
            # Live wire shape (probed 2026-10-14): leg keys are
            # inst/quote/oi with strkPrc, optType, ltp, cur/prev/chg.
            # Documented shape (instrument/quote/openInterest) kept as fallback.
            inst = leg.get("inst", leg.get("instrument", {})) or {}
            q = leg.get("quote", {}) or {}
            oi = leg.get("oi", leg.get("openInterest", {})) or {}
            try:
                strike = int(float(inst.get("strikePrice",
                                            inst.get("strkPrc",
                                                     inst.get("strike")))))
            except (TypeError, ValueError):
                continue
            opt = str(inst.get("optionType", inst.get("optType", "")) or "").upper()
            if opt and opt != side:
                continue
            if not quote_keys:
                quote_keys = sorted(
                    {f"inst.{k}" for k in inst}
                    | {f"quote.{k}" for k in q}
                    | {f"oi.{k}" for k in oi})
            cur = _first(oi, "current", "cur", "oi")
            prev_raw = oi.get("previous", oi.get("prev", None))
            try:
                prev = float(prev_raw) if prev_raw not in (None, "") else None
            except (TypeError, ValueError):
                prev = None
            # ChgOI = current - previous computed locally (matches the
            # documented formula; raw change/chg kept as fallback).
            chg = (cur - prev) if prev is not None else _first(oi, "change", "chg")
            slot = by_strike.setdefault(strike, {})
            slot[side] = {"ltp": _first(q, "ltp"),
                          "oi": cur, "chg": chg}
    strikes = sorted(s for s, v in by_strike.items()
                     if "CE" in v and "PE" in v)
    if not strikes:
        raise ValueError("option_chain: no complete CE/PE strike pairs")

    if spot is None or spot <= 0:
        atm = strikes[len(strikes) // 2]
        warnings = ["spot unavailable — ATM is the middle strike, not live ATM"]
    else:
        atm = min(strikes, key=lambda s: abs(s - spot))
        warnings = []
    centre = strikes.index(atm)
    wanted = strikes[max(0, centre - width):min(len(strikes), centre + width + 1)]

    common = data.get("common_data", {}) or {}
    expiry = str(common.get("expiryDt", "") or "").strip()
    rows = []
    for s in wanted:
        ce, pe = by_strike[s]["CE"], by_strike[s]["PE"]
        rows.append({"strike": s,
                     "callOI": ce["oi"], "putOI": pe["oi"],
                     "callChgOI": ce["chg"], "putChgOI": pe["chg"],
                     "callLTP": ce["ltp"], "putLTP": pe["ltp"],
                     "atm": s == atm})
    return {"underlying": und, "segment": segment, "expiry": expiry,
            "spot": spot, "atm": atm, "rows": rows,
            "warnings": warnings, "quote_keys": quote_keys,
            "source": "option_chain"}


def chain_snapshot(kotak_client: Any, contract_master: Any, *,
                   underlying: str = "NIFTY", segment: str = "nse_fo",
                   spot: Optional[float] = None, width: int = 9,
                   expiry_hint: Optional[str] = None) -> Dict[str, Any]:
    """Fetch live chain rows around ATM for the nearest (or hinted) expiry.

    Returns {"underlying","segment","expiry","spot","atm","rows","warnings",
    "quote_keys"}. Preferred source is the native option_chain endpoint
    (real ChgOI); falls back to quotes (ChgOI = 0 — use total-OI PCR only).
    """
    try:
        snap = _snapshot_via_option_chain(kotak_client, underlying=underlying,
                                          segment=segment, spot=spot,
                                          width=width)
        if snap.get("rows"):
            return snap
    except Exception:
        log.exception("chain_snapshot: option_chain failed, falling back to quotes")
    und = (underlying or "").upper().strip()
    cm = contract_master
    pool = [c for c in (cm._segments.get(segment, []) or [])
            if cm._und_matches(cm._sym_of(c), und)
            and cm._opt_of(c) in ("CE", "PE")]
    if not pool:
        raise ValueError(f"No {und} option contracts in segment {segment}")
    # Standard desk product first (minis only as fallback).
    pool = cm._prefer_standard(pool, und)

    # Expiry resolution: hint → expiries API → master fields (last, they're
    # known-stale). Row matching uses the SYMBOL-embedded expiry first
    # (trustworthy) with row-date equality as backup.
    target = None
    if expiry_hint:
        try:
            target = cm._expiry_hint_to_date(expiry_hint)
            if target is not None and not any(
                    cm._hint_matches(
                        target, cm._symbol_expiry(cm._sym_of(c), und))
                    or cm._row_expiry_date(c) == target for c in pool):
                target = None
        except Exception:
            log.exception("chain_snapshot: expiry hint parse failed")
            target = None
    if target is None:
        target = _nearest_expiry_via_api(kotak_client, segment, und)
    expiry = target.isoformat() if target is not None else None
    if expiry is None:
        expiry = cm._nearest_expiry(pool)
    if not expiry:
        raise ValueError(f"No live expiry found for {und}")
    if target is not None:
        exp_rows = [c for c in pool
                    if cm._hint_matches(
                        target, cm._symbol_expiry(cm._sym_of(c), und))
                    or cm._row_expiry_date(c) == target]
    else:
        exp_rows = [c for c in pool if str(
            c.get("pExpiryDate", "") or c.get("expiry_date", "") or c.get("expiry", "")).strip() == expiry]

    by_strike: Dict[int, Dict[str, Any]] = {}
    for c in exp_rows:
        strike = cm._strike_of(c)
        if strike <= 0:
            continue
        token = _token_of(c)
        if not token:
            continue
        slot = by_strike.setdefault(strike, {})
        slot[cm._opt_of(c)] = {"token": token, "contract": c}
    strikes = sorted(s for s, v in by_strike.items() if "CE" in v and "PE" in v)
    if not strikes:
        raise ValueError(f"No complete CE/PE strike pairs for {und} {expiry}")

    if spot is None or spot <= 0:
        atm = strikes[len(strikes) // 2]
        warnings = ["spot unavailable — ATM is the middle strike, not live ATM"]
    else:
        atm = min(strikes, key=lambda s: abs(s - spot))
        warnings = []
    centre = strikes.index(atm)
    lo = max(0, centre - width)
    hi = min(len(strikes), centre + width + 1)
    wanted = strikes[lo:hi]

    tokens = []
    for s in wanted:
        for side in ("CE", "PE"):
            tokens.append({"exchange_segment": segment,
                           "instrument_token": by_strike[s][side]["token"]})
    try:
        raw = kotak_client.quotes(instrument_tokens=tokens, quote_type="all")
    except Exception:
        log.exception("chain_snapshot: quotes call failed")
        raise ValueError("Kotak quotes call failed — see logs")
    if isinstance(raw, dict) and ("error" in raw or "Error" in raw):
        raise ValueError(f"Kotak quotes error: {raw}")

    items = raw if isinstance(raw, list) else raw.get("data", raw.get("result", []))
    by_token = {}
    quote_keys: List[str] = []
    for q in items or []:
        q = q or {}
        tok = str(q.get("instrument_token", "") or q.get("token", "") or q.get("pSymbol", ""))
        parsed = parse_quote(q)
        by_token[tok] = parsed
        if not quote_keys and parsed["quote_keys"]:
            quote_keys = parsed["quote_keys"]

    rows = []
    for s in wanted:
        ce_tok = by_strike[s]["CE"]["token"]
        pe_tok = by_strike[s]["PE"]["token"]
        ce = by_token.get(ce_tok, parse_quote({}))
        pe = by_token.get(pe_tok, parse_quote({}))
        if ce_tok not in by_token or pe_tok not in by_token:
            warnings.append(f"strike {s}: missing quote leg — zeros used")
        rows.append({"strike": s,
                     "callOI": ce["oi"], "putOI": pe["oi"],
                     "callChgOI": 0, "putChgOI": 0,  # quotes carry no ChgOI
                     "callLTP": ce["ltp"], "putLTP": pe["ltp"],
                     "atm": s == atm})
    return {"underlying": und, "segment": segment, "expiry": expiry,
            "spot": spot, "atm": atm, "rows": rows,
            "warnings": warnings, "quote_keys": quote_keys,
            "source": "quotes"}
