"""
Instrument model + multi-universe signal parser (Phase 1 of multi-instrument engine).

Covers: index options (SENSEX/NIFTY/BANKNIFTY/FINNIFTY), index futures,
and intraday cash equities (NSE CM). Delivery/holdings are out of scope —
the engine is intraday-only (EOD square-off), same as the existing bot.

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
})


@dataclass
class ParsedInstrument:
    """Generic parsed signal across all instrument kinds."""
    status: str                            # 'valid' | 'ignored' | 'empty'
    reason: Optional[str] = None
    kind: str = "OPT"                      # 'OPT' | 'FUT' | 'EQ'
    underlying: Optional[str] = None       # SENSEX/NIFTY/... or equity symbol
    exchange_segment: str = "bse_fo"       # bse_fo | nse_fo | nse_cm
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

    def to_dict(self):
        return asdict(self)

    @property
    def display_symbol(self) -> str:
        """Canonical symbol used for dedup, feed subscription, and trade rows."""
        if self.kind == "OPT":
            return f"{self.underlying}{self.strike}{self.option_type}"
        if self.kind == "FUT":
            return f"{self.underlying}FUT"
        return self.underlying or ""


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
    """Return (canonical, segment) for the first index underlying found.

    Longer names first so 'banknifty' wins over 'nifty'.
    """
    for key in ("bank nifty", "fin nifty", "banknifty", "finnifty", "sensex", "nifty"):
        if key in lower:
            return INDEX_UNDERLYINGS[key]
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
    return s


def parse_instrument_signal(text: str) -> ParsedInstrument:
    """Parse a Telegram signal for any supported instrument kind."""
    if not text or not text.strip():
        return ParsedInstrument(status="empty")

    stripped = _normalize_text(text.strip())
    lower = stripped.lower()

    # ── Must contain a price structure ──
    if not re.search(r"price|@|\bbuy\b|\bsell\b|\bcmp\b|\btgt\b|\btargets?\b|\bsl\b|stoploss",
                     stripped, re.IGNORECASE):
        return ParsedInstrument(status="ignored", reason='No price structure found')

    # ── 1. Index options: underlying + strike + CE/PE ──
    und = _detect_underlying(lower)
    opt_match = re.search(r"(\d{4,6})\s*(CE|PE)\b", stripped, re.IGNORECASE)
    if und and opt_match:
        canonical, segment = und
        strike = opt_match.group(1)
        option_type = opt_match.group(2).upper()
        remaining = stripped[opt_match.end():]
        price_kw = re.search(r"price", remaining, re.IGNORECASE)
        if not price_kw and "@" not in remaining:
            return ParsedInstrument(status="ignored", reason='"price" not found after strike')
        after_price = remaining[price_kw.end():] if price_kw else remaining
        low, high = _parse_entry(after_price)
        if low is None:
            return ParsedInstrument(status="ignored", reason='Entry range not found after "price"')
        return _finish_opt(stripped, lower, canonical, segment, strike, option_type, low, high)

    # ── 2. Index futures: underlying + FUT + price ──
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
        )

    # ── 3. Cash equity: known symbol + price (intraday only) ──
    if not opt_match:
        for sym in sorted(EQUITY_UNIVERSE, key=len, reverse=True):
            if re.search(rf"(?<![A-Z]){sym}(?![A-Z])", stripped.upper()):
                price_kw = re.search(r"price|buy|sell|@|cmp", stripped, re.IGNORECASE)
                if not price_kw:
                    continue
                low, high = _parse_entry(stripped[price_kw.end():])
                if low is None:
                    continue
                avg_match = re.search(r"(?:average|avg)\s*@?\s*(\d{1,6}(?:\.\d+)?)", stripped, re.IGNORECASE)
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
                )

    if opt_match and not und:
        return ParsedInstrument(status="ignored", reason="Option strike found but no known underlying")
    return ParsedInstrument(status="ignored", reason="No supported instrument found")


def _finish_opt(stripped, lower, canonical, segment, strike, option_type, low, high) -> ParsedInstrument:
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
        signal_type=_detect_signal_type(lower),
    )
