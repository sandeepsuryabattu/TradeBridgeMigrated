"""
Instrument model + multi-universe signal parser (Phase 1 of multi-instrument engine).

Covers: index options (SENSEX/NIFTY/BANKNIFTY/FINNIFTY), index futures,
intraday cash equities (NSE CM), and MCX commodity options/futures
(CRUDEOIL/NATURALGAS/GOLD/SILVER/COPPER/...). Delivery/holdings are out of
scope — the engine is intraday-only (EOD square-off), same as the existing bot.

This module is ADDITIVE: the live path still uses `signal_parser.parse_signal`
(SENSEX-only). Nothing here is wired into trade_manager yet (Phase 3).
"""

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

# ── Universe ─────────────────────────────────────────────────────────────────

# Index underlyings: keyword variants -> (canonical, segment)
INDEX_UNDERLYINGS: dict[str, tuple[str, str]] = {
    "sensex":    ("SENSEX", "bse_fo"),
    "banknifty": ("BANKNIFTY", "nse_fo"),
    "bank nifty": ("BANKNIFTY", "nse_fo"),
    "finnifty":  ("FINNIFTY", "nse_fo"),
    "fin nifty": ("FINNIFTY", "nse_fo"),
    "nifty":     ("NIFTY", "nse_fo"),  # keep last: "nifty" is substring of bank/fin nifty
}

EQUITY_SEGMENT = "nse_cm"

MCX_SEGMENT = "mcx_fo"

# MCX commodity underlyings: keyword variants -> (canonical, segment).
# Checked only when no index underlying matches (index is the primary book).
MCX_UNDERLYINGS: dict[str, tuple[str, str]] = {
    "crude oil":   ("CRUDEOIL", MCX_SEGMENT),
    "crudeoil":    ("CRUDEOIL", MCX_SEGMENT),
    "crude":       ("CRUDEOIL", MCX_SEGMENT),
    "natural gas": ("NATURALGAS", MCX_SEGMENT),
    "naturalgas":  ("NATURALGAS", MCX_SEGMENT),
    "nat gas":     ("NATURALGAS", MCX_SEGMENT),
    "natgas":      ("NATURALGAS", MCX_SEGMENT),
    "mentha oil":  ("MENTHAOIL", MCX_SEGMENT),
    "menthaoil":   ("MENTHAOIL", MCX_SEGMENT),
    "aluminium":   ("ALUMINIUM", MCX_SEGMENT),
    "aluminum":    ("ALUMINIUM", MCX_SEGMENT),
    "copper":      ("COPPER", MCX_SEGMENT),
    "nickel":      ("NICKEL", MCX_SEGMENT),
    "silver":      ("SILVER", MCX_SEGMENT),
    "cotton":      ("COTTON", MCX_SEGMENT),
    "gold":        ("GOLD", MCX_SEGMENT),
    "zinc":        ("ZINC", MCX_SEGMENT),
    "lead":        ("LEAD", MCX_SEGMENT),
}

# Canonical MCX underlyings (for segment routing / market-hours checks).
MCX_CANONICALS = frozenset(u for u, _ in MCX_UNDERLYINGS.values())

# Nifty-50-ish cash universe (intraday). Anything outside this set is NOT
# treated as an equity signal — prevents random ALL-CAPS words becoming trades.
EQUITY_UNIVERSE = frozenset({
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "BHARTIARTL", "SBIN",
    "LICI", "LIC", "TITAN", "ITC", "LT", "AXISBANK", "KOTAKBANK", "HINDUNILVR",
    "BAJFINANCE", "MARUTI", "ASIANPAINT", "SUNPHARMA", "NTPC", "TATAMOTORS",
    "TATASTEEL", "POWERGRID", "ULTRACEMCO", "ONGC", "WIPRO", "HCLTECH",
    "BAJAJFINSV", "ADANIENT", "ADANIPORTS", "COALINDIA", "GRASIM", "BPCL",
    "EICHERMOT", "HEROMOTOCO", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP",
    "TATACONSUM", "NESTLEIND", "BRITANNIA", "SHRIRAMFIN", "INDUSINDBK",
    "TECHM", "HINDALCO", "JSWSTEEL", "SBILIFE", "HDFCLIFE", "TRENT", "BEL",
    "TATAPOWER", "VEDL", "GODREJCP", "DABUR", "PIDILITIND", "HAVELLS",
    # Desk equity watch extras (not Nifty-50 but traded on nse_fo).
    "BSE", "M&M",
})


@dataclass
class ParsedInstrument:
    """Generic parsed signal across all instrument kinds."""
    status: str                            # 'valid' | 'ignored' | 'empty'
    reason: Optional[str] = None
    kind: str = "OPT"                      # 'OPT' | 'FUT' | 'EQ'
    underlying: Optional[str] = None       # SENSEX/NIFTY/... or equity symbol
    exchange_segment: str = "bse_fo"       # bse_fo | nse_fo | nse_cm | mcx_fo
    variant: str = "standard"              # 'standard' | 'mini' (MCX minis via MINI keyword)
    strike: Optional[str] = None           # OPT only
    option_type: Optional[str] = None      # OPT only: CE / PE
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    targets: Optional[list] = None
    diff: Optional[float] = None
    stoploss: Optional[float] = None
    expiry: Optional[str] = None
    average: Optional[float] = None
    signal_type: Optional[str] = None
    holding: str = "intraday"              # intraday | short_term | btst | positional
    lots: Optional[int] = None             # explicit "2 lots" / "qty" from message
    entry_trigger: Optional[str] = None    # 'above' | 'below' (Buy above/below X)

    def to_dict(self):
        return asdict(self)

    @property
    def display_symbol(self) -> str:
        """Canonical symbol used for dedup, feed subscription, and trade rows."""
        if self.kind == "OPT":
            mini = "M" if self.variant == "mini" and self.exchange_segment == MCX_SEGMENT else ""
            return f"{self.underlying}{mini}{self.strike}{self.option_type}"
        if self.kind == "FUT":
            return f"{self.underlying}FUT"
        return self.underlying or ""


def _detect_variant(stripped: str) -> str:
    """'mini' when the message asks for the MCX mini contract (MINI keyword)."""
    return "mini" if re.search(r"\bMINI\b", stripped, re.IGNORECASE) else "standard"


def _detect_signal_type(lower: str) -> str:
    if "risky jackpot" in lower:
        return "RISKY JACKPOT"
    if "risky trader" in lower:
        return "RISKY TRADERS"
    if "scalpers" in lower:
        return "SCALPERS"
    if "trading floor" in lower:
        return "TRADING FLOOR"
    return "PLAIN"


def _detect_underlying(lower: str) -> Optional[tuple[str, str]]:
    """Return (canonical, segment) for the first underlying found.

    Index names first (primary book — also avoids 'gold' matching 'golden'
    chatter when an index name is present). Then MCX commodities,
    longest names first so 'crude oil' wins over 'crude'.
    """
    for key in ("bank nifty", "fin nifty", "banknifty", "finnifty", "sensex", "nifty"):
        if key in lower:
            return INDEX_UNDERLYINGS[key]
    for key in sorted(MCX_UNDERLYINGS, key=len, reverse=True):
        if key in lower:
            return MCX_UNDERLYINGS[key]
    return None


def _parse_entry(after_price: str):
    m = re.search(r"@?\s*-?\s*(\d{1,6}(?:\.\d+)?)(?:\s*[-]\s*(\d{1,6}(?:\.\d+)?))?", after_price)
    if not m:
        return None, None
    low = float(m.group(1))
    high = float(m.group(2)) if m.group(2) else low
    return low, high


def _parse_stoploss(stripped: str, entry_high: float) -> Optional[float]:
    sl_match = re.search(
        r"(?<![A-Za-z])(?:stop\s*loss|stoploss|stls|\bsl\b)[^\d]{0,20}(\d{1,6}(?:\.\d+)?)",
        stripped, re.IGNORECASE,
    )
    sl = float(sl_match.group(1)) if sl_match else None
    if sl is not None and sl >= entry_high:
        return None  # almost certainly grabbed a target — drop it
    return sl


def _parse_targets(stripped: str, entry_high: float) -> Optional[list]:
    targets: list[float] = []
    tgt_match = re.search(r"targets?\s*[:\-]?\s*\n?\s*([\d][\d\s/,\.]*)", stripped, re.IGNORECASE)
    if tgt_match:
        raw = tgt_match.group(1).split("\n")[0]  # first line only — never bleed into expiry
        for tok in re.split(r"[/,]", raw):
            tok = tok.strip().split()[0] if tok.strip() else ""
            if tok.replace(".", "", 1).isdigit():
                try:
                    targets.append(float(tok))
                except ValueError:
                    pass
    # Recap cards: "Target 1 ⛔ 150  Target 2 ⛔ 200" — numbered labels
    for m in re.finditer(
        r"target\s*\d+\s*[⛔️︎\ufe0f:\-–—]+\s*(\d+(?:\.\d+)?)", stripped, re.IGNORECASE
    ):
        try:
            targets.append(float(m.group(1)))
        except ValueError:
            pass
    # Single-line style: "TGT 180/220", "Tgt: 180, 220"
    for m in re.finditer(
        r"\btgt\s*[:\-]?\s*([\d][\d\s/,\.]*)", stripped, re.IGNORECASE
    ):
        raw = m.group(1).split("\n")[0]
        for tok in re.split(r"[/,]", raw):
            tok = tok.strip().split()[0] if tok.strip() else ""
            if tok.replace(".", "", 1).isdigit():
                try:
                    targets.append(float(tok))
                except ValueError:
                    pass
    for m in re.finditer(r"\bT\s*([1-5])\s*[:\-]?\s*(\d+(?:\.\d+)?)", stripped):
        try:
            targets.append(float(m.group(2)))
        except ValueError:
            pass
    targets = [t for t in targets if t > entry_high]
    return targets[:5] if targets else None


def _parse_expiry(stripped: str) -> Optional[str]:
    m = re.search(r"(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s+expiry)", stripped, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _normalize_text(stripped: str) -> str:
    """Normalize recap-format variants to the canonical signal vocabulary.

    - CALL/PUT -> CE/PE (INDEX OPTIONS recap cards use CALL/PUT)
    - Fancy dashes (➖ U+2796, en/em dash, minus sign) -> hyphen
    """
    s = re.sub(r"\bCALL\b", "CE", stripped, flags=re.IGNORECASE)
    s = re.sub(r"\bPUT\b", "PE", s, flags=re.IGNORECASE)
    s = re.sub(r"[➖–—−]", "-", s)
    s = re.sub(r"(?<=\d),(?=\d)", "", s)  # 24,620 -> 24620
    return s


# Words that look like equity symbols but are actually instructions/markets.
_NON_SYMBOLS = frozenset({
    "ABOVE", "BELOW", "AT", "CMP", "MORE", "LOT", "LOTS", "QTY", "SHARES",
    "RS", "TARGET", "TARGETS", "ENTRY", "PRICE", "CALL", "PUT", "FUT",
    "BUY", "SELL", "HOLD", "ADD", "WITH", "FROM", "NEAR", "AROUND", "ZONE",
    "NIFTY", "SENSEX", "BANKNIFTY", "FINNIFTY", "BANK", "NIFTY50",
    # MCX commodities must never be claimed as cash-equity symbols.
    "CRUDEOIL", "CRUDE", "NATURALGAS", "NATGAS", "GOLD", "SILVER", "COPPER",
    "ALUMINIUM", "ALUMINUM", "ZINC", "LEAD", "NICKEL", "MENTHAOIL", "COTTON",
    "MINI",
    # Channel headers / meta words — never a stock symbol.
    "AUTOTRENDER", "AUTOTREND", "INDEX", "OPTIONS", "OPTION", "EXPIRY",
})


def _find_stock_option_symbol(stripped: str, opt_start: int) -> Optional[str]:
    """Find a single-stock symbol for an equity option (INFY 1160CE).

    Looks at text BEFORE the strike match:
    1. Prefer a known EQUITY_UNIVERSE member (last occurrence wins).
    2. Fall back to the last ALL-CAPS token (excludes _NON_SYMBOLS).
    Returns None when nothing stock-like precedes the strike.
    """
    before = stripped[:opt_start]
    upper_before = before.upper()
    # "M&M" contains a non-word char so word-boundary regexes can never
    # match it — check literally first.
    if "M&M" in upper_before:
        return "M&M"
    best: Optional[str] = None
    best_pos = -1
    for sym in EQUITY_UNIVERSE:
        for m in re.finditer(rf"(?<![A-Z]){re.escape(sym)}(?![A-Z])", upper_before):
            if m.start() > best_pos:
                best, best_pos = sym, m.start()
    if best:
        return best
    cands: list[tuple[int, str]] = []
    for m in re.finditer(r"\b([A-Z][A-Z0-9]{1,13})\b", upper_before):
        sym = m.group(1)
        if sym in _NON_SYMBOLS:
            continue
        if not any(c.isalpha() for c in sym):
            continue
        cands.append((m.start(), sym))
    if not cands:
        return None
    return cands[-1][1]

_FOLLOWUP_RE = re.compile(
    r"ACHIEV|POINT'?S?\s+DONE|\bDONE\b|\bHIT\b|\bBOOK\b|»|ACTIVATED|TARGET\s+\d+\s+DONE",
    re.IGNORECASE,
)

_HOLDING_PATTERNS: list[tuple[str, str]] = [
    (r"\bSHORT\s+TERM\b|\bSWING\b|\bPOSITIONAL\b|\b\d+\s*-\s*\d+\s*TRADING\s+DAYS\b", "short_term"),
    (r"\bBTST\b", "btst"),
    (r"\bSTBT\b", "btst"),
    (r"\bINTRADAY\b|\bSCALP", "intraday"),
]


def _detect_holding(stripped: str) -> str:
    for pat, tag in _HOLDING_PATTERNS:
        if re.search(pat, stripped, re.IGNORECASE):
            return tag
    return "intraday"


def _detect_lots(stripped: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*LOTS?\b", stripped, re.IGNORECASE)
    if m:
        try:
            return max(1, int(m.group(1)))
        except ValueError:
            pass
    m = re.search(r"\bQTY\s*[:\-]?\s*(\d+)", stripped, re.IGNORECASE)
    if m:
        try:
            return max(1, int(m.group(1)))
        except ValueError:
            pass
    return None


def parse_instrument_signal(text: str) -> ParsedInstrument:
    """Parse a Telegram signal for any supported instrument kind."""
    if not text or not text.strip():
        return ParsedInstrument(status="empty")

    stripped = _normalize_text(text.strip())
    lower = stripped.lower()

    # ── Follow-up/commentary guard: celebrations, activations, admin notes ──
    # Entry signals never contain these; recap/entry cards always carry an
    # entry-price structure instead.
    if _FOLLOWUP_RE.search(stripped):
        return ParsedInstrument(status="ignored", reason="Follow-up/commentary, not an entry")

    # ── Must contain a price structure ──
    # MCX desk messages are terse ("CRUDEOIL 9000 PE 186") — the trailing
    # price counts as the structure for commodities.
    if not re.search(r"price|@|\bbuy\b|\bsell\b|\bcmp\b|\btgt\b|\btargets?\b|\bsl\b|stoploss",
                     stripped, re.IGNORECASE):
        _gate_und = _detect_underlying(lower)
        if not (_gate_und and _gate_und[1] == MCX_SEGMENT):
            return ParsedInstrument(status="ignored", reason='No price structure found')

    # ── 1. Index/commodity options: underlying + strike + CE/PE ──
    und = _detect_underlying(lower)
    # MCX strikes can be 3 digits (e.g. NATURALGAS 220); index strikes are 4+.
    opt_match = re.search(r"(\d{3,6})\s*(CE|PE)\b", stripped, re.IGNORECASE)
    if und and opt_match:
        canonical, segment = und
        strike = opt_match.group(1)
        option_type = opt_match.group(2).upper()
        remaining = stripped[opt_match.end():]
        price_kw = re.search(r"price|buy|sell|entry|@|cmp", remaining, re.IGNORECASE)
        if not price_kw:
            # Terse MCX desk format: "CRUDEOIL 9000 PE 186" or
            # "CRUDEOIL 9000 PE 180-190" — first bare number(s) right after
            # the strike are the entry (anchored: a trailing SL like
            # "... 186 SL 150" must not become the entry). Index signals
            # still require a price keyword (avoids turning chatter
            # into trades).
            if segment == MCX_SEGMENT:
                rest = remaining.strip()
                m2 = re.match(
                    r"(-?\d{1,6}(?:\.\d+)?)\s*[-/ ]\s*(-?\d{1,6}(?:\.\d+)?)", rest)
                if m2:
                    lo, hi = float(m2.group(1)), float(m2.group(2))
                    if hi < lo:
                        lo, hi = hi, lo
                    return _finish_opt(stripped, lower, canonical, segment,
                                      strike, option_type, lo, hi,
                                      _detect_variant(stripped))
                m1 = re.match(r"(-?\d{1,6}(?:\.\d+)?)", rest)
                if m1:
                    px = float(m1.group(1))
                    return _finish_opt(stripped, lower, canonical, segment,
                                      strike, option_type, px, px,
                                      _detect_variant(stripped))
            return ParsedInstrument(status="ignored", reason='"price" not found after strike')
        after_price = remaining[price_kw.end():] if price_kw else remaining
        low, high = _parse_entry(after_price)
        if low is None:
            return ParsedInstrument(status="ignored", reason='Entry range not found after "price"')
        return _finish_opt(stripped, lower, canonical, segment, strike, option_type, low, high,
                           _detect_variant(stripped))

    # ── 1b. Single-stock equity options: <STOCK> + strike + CE/PE (nse_fo) ──
    # e.g. "INFY\n1160CE\nPRICE @ 28.71-29.89..." — _detect_underlying only
    # knows indices/commodities, so stock options fell through to the
    # "Option strike found but no known underlying" ignore (live 2026-10-16).
    if opt_match and not und:
        stock = _find_stock_option_symbol(stripped, opt_match.start())
        if stock:
            strike = opt_match.group(1)
            option_type = opt_match.group(2).upper()
            remaining = stripped[opt_match.end():]
            price_kw = re.search(r"price|buy|sell|entry|@|cmp", remaining, re.IGNORECASE)
            if not price_kw:
                return ParsedInstrument(status="ignored", reason='"price" not found after strike')
            after_price = remaining[price_kw.end():]
            low, high = _parse_entry(after_price)
            if low is None:
                return ParsedInstrument(status="ignored", reason='Entry range not found after "price"')
            return _finish_opt(stripped, lower, stock, "nse_fo", strike, option_type, low, high,
                               _detect_variant(stripped))

    # ── 2. Index/commodity futures: underlying + FUT + price ──
    if und and re.search(r"\bFUT\b", stripped, re.IGNORECASE):
        canonical, segment = und
        price_kw = re.search(r"price", stripped, re.IGNORECASE)
        after_price = stripped[price_kw.end():] if price_kw else stripped
        low, high = _parse_entry(after_price)
        if low is None:
            return ParsedInstrument(status="ignored", reason="Entry range not found for future")
        avg_match = re.search(r"(?:average|avg)\s*@?\s*(\d{1,6}(?:\.\d+)?)", stripped, re.IGNORECASE)
        average = float(avg_match.group(1)) if avg_match else None
        if average is not None:
            low = average
        sl = _parse_stoploss(stripped, high)
        targets = _parse_targets(stripped, high)
        if high < low:
            return ParsedInstrument(status="ignored", reason="High < Low")
        return ParsedInstrument(
            status="valid", kind="FUT", underlying=canonical,
            exchange_segment=segment, entry_low=low, entry_high=high,
            targets=targets, diff=abs(high - low), stoploss=sl,
            expiry=_parse_expiry(stripped), average=average,
            signal_type=_detect_signal_type(lower),
            variant=_detect_variant(stripped),
        )

    # ── 3. Cash equity: known symbol or BUY/SELL <SYMBOL> <price> (intraday) ──
    if not opt_match:
        candidates: list[str] = []
        for sym in sorted(EQUITY_UNIVERSE, key=len, reverse=True):
            if re.search(rf"(?<![A-Z]){sym}(?![A-Z])", stripped.upper()):
                candidates.append(sym)
        # Generic channel style: "INTRADAY BUY 360ONE 1197", "SHORT TERM BUY KPRMILL 1135"
        for m in re.finditer(
            r"(?:INTRADAY|SHORT\s+TERM|BTST|STBT|SCALP\s+)?\s*(?:BUY|SELL)\s+([A-Z0-9]{2,14})\b",
            stripped.upper(),
        ):
            sym = m.group(1)
            if not any(c.isalpha() for c in sym):
                continue  # pure number (a price), not a symbol
            if sym not in _NON_SYMBOLS and sym not in candidates:
                candidates.append(sym)
        for sym in candidates:
            # "Buy above 24620" / "Sell below 590" trigger style
            trig = None
            tm = re.search(rf"\bBUY\s+ABOVE\b|\bSELL\s+BELOW\b", stripped, re.IGNORECASE)
            if tm:
                trig = "above" if "ABOVE" in tm.group(0).upper() else "below"
            # Parse entry from AFTER the symbol so digits inside the symbol
            # itself (360ONE -> 360) can never be mistaken for the price.
            sym_at = stripped.upper().find(sym)
            entry_src = stripped[sym_at + len(sym):] if sym_at >= 0 else stripped
            # Slash entry range right after the symbol: "SPECIALITY 146.50/147"
            low, high = None, None
            sm = re.search(
                r"^\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", entry_src
            )
            if sm:
                low, high = float(sm.group(1)), float(sm.group(2))
            if low is None:
                low, high = _parse_entry(entry_src)
                if low is None:
                    continue
            # "add more 1192" acts as the average override for equities
            avg_match = re.search(
                r"(?:average|avg|add\s+more)\s*@?\s*(\d{1,6}(?:\.\d+)?)",
                stripped, re.IGNORECASE,
            )
            average = float(avg_match.group(1)) if avg_match else None
            if average is not None:
                low = average
            sl = _parse_stoploss(stripped, high)
            targets = _parse_targets(stripped, high)
            if high < low:
                return ParsedInstrument(status="ignored", reason="High < Low")
            return ParsedInstrument(
                status="valid", kind="EQ", underlying=sym,
                exchange_segment=EQUITY_SEGMENT, entry_low=low, entry_high=high,
                targets=targets, diff=abs(high - low), stoploss=sl,
                expiry=None, average=average,
                signal_type=_detect_signal_type(lower),
                holding=_detect_holding(stripped), lots=_detect_lots(stripped),
                entry_trigger=trig,
            )

    if opt_match and not und:
        return ParsedInstrument(status="ignored", reason="Option strike found but no known underlying")
    return ParsedInstrument(status="ignored", reason="No supported instrument found")


def _finish_opt(stripped, lower, canonical, segment, strike, option_type, low, high,
                variant: str = "standard") -> ParsedInstrument:
    avg_match = re.search(r"(?:average|avg)\s*@?\s*(\d{1,5})", stripped, re.IGNORECASE)
    average = None
    if avg_match:
        average = float(avg_match.group(1))
        low = average
    sl = _parse_stoploss(stripped, high)
    targets = _parse_targets(stripped, high)
    if high < low:
        return ParsedInstrument(status="ignored", reason="High < Low")
    if abs(high - low) > 50 and canonical == "SENSEX":
        # Legacy SENSEX guard (option premiums are small); NIFTY/BANKNIFTY
        # weekly premiums can be wider so the cap only applies to SENSEX.
        return ParsedInstrument(status="ignored", reason="Range difference > 50")
    return ParsedInstrument(
        status="valid", kind="OPT", underlying=canonical,
        exchange_segment=segment, strike=strike, option_type=option_type,
        entry_low=float(low), entry_high=float(high),
        targets=targets, diff=float(abs(high - low)), stoploss=sl,
        expiry=_parse_expiry(stripped), average=average,
        signal_type=_detect_signal_type(lower), variant=variant,
    )
