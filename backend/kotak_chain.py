"""
Kotak chain snapshots — live option-chain rows for the autotrender engine.

Source: Kotak Neo `quotes` REST API (quote_type='all') over NSE_FO option
tokens enumerated from the contract master. No new credentials, no new
packages — reuses the authenticated `kotak_trader.client` and the downloaded
contract master already used for order routing.

Honest limits (verified against installed neo_api_client):
- `quotes` returns a per-instrument snapshot: LTP, day OHLC, volume and —
  for derivatives — open interest. It does NOT return change-in-OI, so
  engine rows carry callChgOI/putChgOI = 0 and PCR signals must use total-OI
  PCR (calc_pcr), never calc_pcr_chg (0/0 would read as STRONG SELL).
- No historical-candles endpoint exists in this client: multi-day history is
  unavailable; intraday candles come from candle_builder (live ticks).
- Quote field names are normalised defensively: every parsed row records
  `quote_keys` (sorted raw keys) so the VM's first live response calibrates
  the parser — check logs, extend _FIRST() maps if a field lands at 0.

Nothing here trades. Output rows are engine-ready
(strike/callOI/putOI/callLTP/putLTP/atm) for autotrender_engine.
"""
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


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


def chain_snapshot(kotak_client: Any, contract_master: Any, *,
                   underlying: str = "NIFTY", segment: str = "nse_fo",
                   spot: Optional[float] = None, width: int = 9,
                   expiry_hint: Optional[str] = None) -> Dict[str, Any]:
    """Fetch live chain rows around ATM for the nearest (or hinted) expiry.

    Returns {"underlying","segment","expiry","spot","atm","rows","warnings",
    "quote_keys"}. rows are engine-ready; callChgOI/putChgOI are 0 (see
    module docstring — use total-OI PCR only).
    """
    und = (underlying or "").upper().strip()
    cm = contract_master
    pool = [c for c in (cm._segments.get(segment, []) or [])
            if cm._und_matches(cm._sym_of(c), und)
            and cm._opt_of(c) in ("CE", "PE")]
    if not pool:
        raise ValueError(f"No {und} option contracts in segment {segment}")
    # Standard desk product first (minis only as fallback).
    pool = cm._prefer_standard(pool, und)

    expiry = None
    if expiry_hint:
        try:
            hint = cm._expiry_hint_to_date(expiry_hint)
            if hint is not None:
                for c in pool:
                    rd = cm._row_expiry_date(c)
                    if rd is not None and cm._hint_matches(hint, rd):
                        expiry = str(c.get("pExpiryDate", "") or c.get("expiry_date", "")
                                     or c.get("expiry", "")).strip()
                        break
        except Exception:
            log.exception("chain_snapshot: expiry hint parse failed")
    if not expiry:
        expiry = cm._nearest_expiry(pool)
    if not expiry:
        raise ValueError(f"No live expiry found for {und}")
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
            "warnings": warnings, "quote_keys": quote_keys}
