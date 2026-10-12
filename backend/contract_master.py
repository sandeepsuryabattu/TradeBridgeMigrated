"""
Contract Master — Downloads and parses the daily BSE contract master from Kotak Neo.
Filters to only SENSEX options for the nearest expiry (~450 contracts).
Used to resolve human-readable signals (e.g. SENSEX 80000 PE) to real instrument tokens.
"""
import csv
import io
import os
import re
import logging
import httpx
from datetime import datetime, date
from typing import Optional

log = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
MASTER_FILE = os.path.join(DATA_DIR, "sensex_contracts.csv")


class ContractMaster:
    """Manages the daily contract master for SENSEX options."""

    def __init__(self):
        self._contracts: list[dict] = []
        self._lookup_index: dict[tuple, dict] = {}   # (strike_str, option_type) -> contract
        self._last_download: Optional[date] = None
        # Phase 2: per-segment masters for the multi-instrument engine.
        # Keys: bse_fo / nse_fo / nse_cm. The legacy SENSEX-only
        # _contracts/_lookup_index above are left untouched.
        self._segments: dict[str, list[dict]] = {}

    @property
    def is_loaded(self) -> bool:
        return len(self._contracts) > 0

    def download(self, kotak_client) -> bool:
        """Download the full scrip master from Kotak Neo and filter to SENSEX nearest-expiry options.
        On failure, falls back to the last saved CSV so trading continues uninterrupted.
        """
        if not kotak_client or not kotak_client.client:
            log.warning("Cannot download contract master — Kotak client not available")
            return self._fallback_to_csv()

        try:
            log.info("Downloading scrip master from Kotak Neo...")
            result = kotak_client.client.scrip_master(exchange_segment="bse_fo")

            if not result:
                log.error("scrip_master() returned empty result")
                return self._fallback_to_csv()

            # Parse the result — it may be a URL, CSV string, list of dicts, etc.
            contracts = self._parse_scrip_master(result)

            if not contracts:
                log.error("No contracts parsed from scrip master")
                return self._fallback_to_csv()

            log.info(f"Total BSE FO contracts downloaded: {len(contracts)}")

            # Filter to SENSEX options only
            sensex_contracts = [
                c for c in contracts
                if self._is_sensex_option(c)
            ]
            log.info(f"SENSEX options found: {len(sensex_contracts)}")

            if not sensex_contracts:
                log.warning("No SENSEX options found in contract master!")
                return self._fallback_to_csv()

            # Find the nearest expiry
            nearest_expiry = self._find_nearest_expiry(sensex_contracts)
            if not nearest_expiry:
                log.error("Could not determine nearest expiry")
                return self._fallback_to_csv()

            log.info(f"Nearest SENSEX expiry: {nearest_expiry}")

            # Filter to only nearest expiry
            self._contracts = [
                c for c in sensex_contracts
                if str(c.get("pExpiryDate", "") or c.get("expiry_date", "") or c.get("expiry", "")).strip() == nearest_expiry
            ]

            log.info(f"Final SENSEX contracts (nearest expiry): {len(self._contracts)}")
            self._last_download = date.today()
            self._build_lookup_index()

            # Save filtered contracts to CSV for reference
            self._save_to_csv()

            return True

        except Exception as e:
            log.error(f"Contract master download failed: {e}")
            return self._fallback_to_csv()

    def _fallback_to_csv(self) -> bool:
        """Fall back to the last saved CSV if the live download fails.
        Ensures _contracts is never left empty after a failed refresh.
        """
        if self._contracts:
            log.info(f"Download failed but {len(self._contracts)} contracts already in memory — keeping them")
            return True
        log.warning("Download failed and no contracts in memory — attempting CSV fallback")
        return self._load_from_csv()

    def load_cached(self) -> bool:
        """Load contracts from the saved CSV file (called on startup before first live download)."""
        return self._load_from_csv()

    def _parse_scrip_master(self, result) -> list[dict]:
        """Parse the scrip master result into a list of contract dicts.
        
        Kotak Neo's scrip_master() can return:
        - A URL string pointing to a CSV file (most common)
        - A raw CSV string
        - A list of dicts
        - A dict with a 'data' key
        """
        contracts = []

        try:
            if isinstance(result, list):
                # Already a list of dicts
                contracts = result
            elif isinstance(result, str):
                # Check if it's a URL (Kotak returns a URL to the CSV file)
                if result.strip().startswith("http"):
                    log.info(f"scrip_master returned URL: {result.strip()}")
                    csv_data = self._download_csv_from_url(result.strip())
                    if csv_data:
                        reader = csv.DictReader(io.StringIO(csv_data))
                        contracts = list(reader)
                        log.info(f"Parsed {len(contracts)} contracts from downloaded CSV")
                else:
                    # Raw CSV string — parse it
                    reader = csv.DictReader(io.StringIO(result))
                    contracts = list(reader)
            elif isinstance(result, dict):
                # May contain a 'data' key with the actual list
                data = result.get("data", result)
                if isinstance(data, list):
                    contracts = data
                elif isinstance(data, str):
                    if data.strip().startswith("http"):
                        csv_data = self._download_csv_from_url(data.strip())
                        if csv_data:
                            reader = csv.DictReader(io.StringIO(csv_data))
                            contracts = list(reader)
                    else:
                        reader = csv.DictReader(io.StringIO(data))
                        contracts = list(reader)
        except Exception as e:
            log.error(f"Error parsing scrip master: {e}")

        return contracts

    def _download_csv_from_url(self, url: str) -> Optional[str]:
        """Download CSV content from a URL."""
        try:
            log.info(f"Downloading CSV from {url}...")
            response = httpx.get(url, timeout=30.0)
            response.raise_for_status()
            log.info(f"Downloaded {len(response.text)} bytes of CSV data")
            return response.text
        except Exception as e:
            log.error(f"Failed to download CSV from {url}: {e}")
            return None

    def _is_sensex_option(self, contract: dict) -> bool:
        """Check if a contract is a SENSEX option (CE/PE)."""
        # Kotak Neo scrip master uses various field names
        # When downloading from CSV, field names come from CSV headers
        # When from search_scrip, field names use pXxx convention
        symbol = str(
            contract.get("pSymbolName", "") or
            contract.get("pSymbol", "") or
            contract.get("symbol", "") or
            contract.get("pScripRefKey", "") or
            contract.get("pTrdSymbol", "") or
            ""
        ).upper()

        option_type = str(
            contract.get("pOptionType", "") or
            contract.get("option_type", "") or
            ""
        ).upper()

        # Must be SENSEX and must be an option (CE or PE)
        is_sensex = "SENSEX" in symbol or "BSXOPT" in symbol
        is_option = option_type in ("CE", "PE")

        return is_sensex and is_option

    def _find_nearest_expiry(self, contracts: list[dict]) -> Optional[str]:
        """Find the nearest future expiry date from the contracts.
        
        Handles both:
        - Unix timestamps (from CSV download): "1776364199"
        - Date strings (from API): "16Apr2026", "16-Apr-2026", etc.
        """
        today = date.today()
        expiry_map: dict[str, date] = {}  # raw string -> parsed date

        for c in contracts:
            exp_str = str(c.get("pExpiryDate", "") or c.get("expiry_date", "") or c.get("expiry", "")).strip()
            if not exp_str:
                continue

            if exp_str in expiry_map:
                continue  # Already parsed

            exp_date = None
            try:
                # Try Unix timestamp first (purely numeric or numeric-like)
                ts = float(exp_str)
                if ts > 1_000_000_000:  # Looks like a Unix timestamp
                    exp_date = datetime.fromtimestamp(ts).date()
            except (ValueError, TypeError, OSError):
                pass

            if exp_date is None:
                # Try common date string formats
                for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d%b%Y"):
                    try:
                        exp_date = datetime.strptime(exp_str, fmt).date()
                        break
                    except ValueError:
                        continue

            if exp_date and exp_date >= today:
                expiry_map[exp_str] = exp_date

        if not expiry_map:
            return None

        # Return the nearest future expiry
        return min(expiry_map.keys(), key=lambda s: expiry_map[s])

    def lookup(self, strike: str, option_type: str) -> Optional[dict]:
        """Look up the instrument token for a SENSEX option by strike and type.
        O(1) via _lookup_index; falls back to linear scan if index not built.

        Args:
            strike: Strike price as string, e.g. "80000"
            option_type: "CE" or "PE"

        Returns:
            dict with instrument_token, trading_symbol, etc. or None
        """
        option_type = option_type.upper()

        # Fast path — O(1) dict lookup
        if self._lookup_index:
            try:
                strike_val = str(int(float(strike)))
                c = self._lookup_index.get((strike_val, option_type))
                if c:
                    return {
                        "instrument_token": str(c.get("pSymbol", "") or c.get("pInstrumentToken", "") or c.get("instrument_token", "")),
                        "trading_symbol": c.get("pTrdSymbol", "") or c.get("trading_symbol", ""),
                        "exchange_segment": "bse_fo",
                        "strike": strike,
                        "option_type": option_type,
                        "expiry": c.get("pExpiryDate", "") or c.get("expiry_date", ""),
                        "lot_size": c.get("lLotSize", "") or c.get("pLotSize", "") or c.get("lot_size", ""),
                        "tick_size": c.get("dTickSize", "") or c.get("pTickSize", "") or c.get("tick_size", ""),
                        "raw": c,
                    }
            except (ValueError, TypeError):
                pass  # fall through to linear scan

        # Slow path — O(n) linear scan (fallback when index not built)
        for c in self._contracts:
            # Kotak has a quirky field name: "dStrikePrice;" (with semicolon)
            c_strike = str(
                c.get("dStrikePrice;", "") or
                c.get("pStrikePrice", "") or
                c.get("strike_price", "") or
                c.get("dStrikePrice", "") or
                ""
            ).strip()
            c_option = str(c.get("pOptionType", "") or c.get("option_type", "") or "").upper()

            # Remove decimals from strike for comparison (e.g. "8000000.0" -> "80000")
            # Kotak stores strike * 100, so "8000000.0" means strike 80000
            c_strike_clean = c_strike.split(".")[0] if "." in c_strike else c_strike
            strike_clean = strike.split(".")[0] if "." in strike else strike

            # Handle Kotak's strike * 100 convention
            try:
                c_strike_val = int(float(c_strike)) if c_strike else 0
                strike_val = int(strike_clean) if strike_clean else 0
                # If the contract strike is 100x the signal strike, adjust
                if c_strike_val == strike_val * 100:
                    c_strike_clean = str(strike_val)
                elif c_strike_val == strike_val:
                    c_strike_clean = str(strike_val)
                else:
                    c_strike_clean = str(c_strike_val)
                strike_clean = str(strike_val)
            except (ValueError, TypeError):
                pass

            if c_strike_clean == strike_clean and c_option == option_type:
                return {
                    "instrument_token": str(c.get("pSymbol", "") or c.get("pInstrumentToken", "") or c.get("instrument_token", "")),
                    "trading_symbol": c.get("pTrdSymbol", "") or c.get("trading_symbol", ""),
                    "exchange_segment": "bse_fo",
                    "strike": strike,
                    "option_type": option_type,
                    "expiry": c.get("pExpiryDate", "") or c.get("expiry_date", ""),
                    "lot_size": c.get("lLotSize", "") or c.get("pLotSize", "") or c.get("lot_size", ""),
                    "tick_size": c.get("dTickSize", "") or c.get("pTickSize", "") or c.get("tick_size", ""),
                    "raw": c,  # Keep the full contract data for debugging
                }

        log.warning(f"No contract found for SENSEX {strike} {option_type}")
        return None

    def get_all(self) -> list[dict]:
        """Return all loaded contracts."""
        return list(self._contracts)

    # ── Phase 2: multi-universe masters ──────────────────────────────────────

    MULTI_SEGMENTS = ("bse_fo", "nse_fo", "nse_cm")

    def download_all(self, kotak_client, segments=None) -> dict[str, bool]:
        """Download scrip masters for every segment (BSE+NSE FO, NSE CM).

        Failures fall back per-segment: in-memory contracts are kept, else the
        legacy SENSEX CSV fallback is attempted for bse_fo. Returns per-segment
        success flags. Never raises.
        """
        results: dict[str, bool] = {}
        for seg in (segments or self.MULTI_SEGMENTS):
            try:
                result = kotak_client.client.scrip_master(exchange_segment=seg)
                contracts = self._parse_scrip_master(result)
                if contracts:
                    self._segments[seg] = contracts
                    log.info("Segment %s: %d contracts", seg, len(contracts))
                    results[seg] = True
                else:
                    log.warning("Segment %s: empty master — keeping prior data", seg)
                    results[seg] = bool(self._segments.get(seg))
            except Exception:
                log.exception("Segment %s: download failed", seg)
                results[seg] = bool(self._segments.get(seg))
        if not self._segments.get("bse_fo") and not self._contracts:
            self._fallback_to_csv()
        return results

    @staticmethod
    def _sym_of(contract: dict) -> str:
        return str(
            contract.get("pTrdSymbol", "") or contract.get("pSymbolName", "") or
            contract.get("pSymbol", "") or contract.get("symbol", "") or ""
        ).upper()

    @staticmethod
    def _strike_of(contract: dict) -> int:
        raw = str(
            contract.get("dStrikePrice;", "") or contract.get("pStrikePrice", "") or
            contract.get("strike_price", "") or contract.get("dStrikePrice", "") or ""
        ).strip()
        try:
            val = int(float(raw)) if raw else 0
        except (ValueError, TypeError):
            return 0
        return val // 100 if val > 99999 else val  # Kotak strike*100 convention

    @staticmethod
    def _opt_of(contract: dict) -> str:
        return str(contract.get("pOptionType", "") or contract.get("option_type", "") or "").upper()

    def _nearest_expiry(self, contracts: list[dict]) -> Optional[str]:
        raws = {
            str(c.get("pExpiryDate", "") or c.get("expiry_date", "") or c.get("expiry", "")).strip()
            for c in contracts
        }
        raws.discard("")
        today = date.today()
        parsed: dict[str, date] = {}
        for exp_str in raws:
            exp_date = None
            try:
                ts = float(exp_str)
                if ts > 1_000_000_000:
                    exp_date = datetime.fromtimestamp(ts).date()
            except (ValueError, TypeError, OSError):
                pass
            if exp_date is None:
                for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d%b%Y"):
                    try:
                        exp_date = datetime.strptime(exp_str, fmt).date()
                        break
                    except ValueError:
                        continue
            if exp_date and exp_date >= today:
                parsed[exp_str] = exp_date
        if not parsed:
            return None
        return min(parsed.keys(), key=lambda s: parsed[s])

    KNOWN_UNDERLYINGS = ("BANKNIFTY", "FINNIFTY", "SENSEX", "NIFTY")

    @classmethod
    def _und_matches(cls, sym: str, und: str) -> bool:
        """True when sym belongs to underlying und — never confusing NIFTY
        with FINNIFTY/BANKNIFTY (live bug, 2026-10-12).

        Longest-known-underlying wins, so weekly infixes (NIFTYWK13…) still
        match while FINNIFTY… resolves to FINNIFTY, not NIFTY.
        """
        best = ""
        for u in cls.KNOWN_UNDERLYINGS:
            if sym == u or sym.startswith(u):
                if len(u) > len(best):
                    best = u
        if best:
            return best == und
        # Unknown underlying: prefix + digit/separator heuristic.
        if not sym.startswith(und):
            return False
        if len(sym) == len(und):
            return True
        return sym[len(und)].isdigit() or sym[len(und)] in " -_"

    _MONTHS = {m: i + 1 for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"])}
    _MONTHS.update({m[:3]: i + 1 for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"])})

    @classmethod
    def _expiry_hint_to_date(cls, hint: str) -> Optional[date]:
        """Parse signal expiry text ("13 october expiry", "23rd APRIL EXPIRY")
        into a date. Rolls the year forward when the date already passed."""
        if not hint:
            return None
        m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)", hint)
        if not m:
            return None
        month = cls._MONTHS.get(m.group(2).lower())
        if not month:
            return None
        today = date.today()
        try:
            d = date(today.year, month, int(m.group(1)))
        except ValueError:
            return None
        if d < today:
            try:
                d = date(today.year + 1, month, int(m.group(1)))
            except ValueError:
                return None
        return d

    # Single-letter month codes (OCC-style + Kotak's O=Oct, D=Dec variants).
    _MON1 = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6, "N": 7,
             "Q": 8, "U": 9, "V": 10, "O": 10, "X": 11, "Z": 12, "D": 12}

    @staticmethod
    def _row_expiry_date(contract: dict) -> Optional[date]:
        raw = str(contract.get("pExpiryDate", "") or contract.get("expiry_date", "")
                  or contract.get("expiry", "")).strip()
        if not raw:
            return None
        try:
            ts = float(raw)
            if ts > 1_000_000_000:
                return datetime.fromtimestamp(ts).date()
        except (ValueError, TypeError, OSError, OverflowError):
            pass
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d%b%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    @classmethod
    def _symbol_expiry(cls, sym: str, und: str):
        """Extract (year, month, day|None) embedded in the broker symbol.

        NIFTY26OCT22450CE -> (2026, 10, None); NIFTY26O1322450CE -> (2026, 10, 13).
        Kotak's row expiry fields are unreliable (stale 2016 timestamps), so
        the symbol is the trustworthy date source. Returns None if unparseable.
        """
        rest = sym[len(und):] if sym.startswith(und) else sym
        # Monthly: UND26OCT<strike> (no day). Weekly: UND26O13<strike>.
        # A 3-letter month must NOT swallow strike digits as a day.
        m = re.match(r"(\d{2})([A-Z]{3})", rest)
        if m:
            month = cls._MONTHS.get(m.group(2).lower())
            if not month:
                return None
            return (2000 + int(m.group(1)), month, None)
        m = re.match(r"(\d{2})([A-Z])(\d{1,2})?", rest)
        if not m:
            return None
        month = cls._MON1.get(m.group(2))
        if not month:
            return None
        day = int(m.group(3)) if m.group(3) else None
        return (2000 + int(m.group(1)), month, day)

    @staticmethod
    def _hint_matches(hint: date, sym_date) -> bool:
        """A symbol with day must match exactly; month-only must match month."""
        if sym_date is None:
            return False
        y, m, d = sym_date
        if y != hint.year or m != hint.month:
            return False
        return d is None or d == hint.day
        raw = str(contract.get("pExpiryDate", "") or contract.get("expiry_date", "")
                  or contract.get("expiry", "")).strip()
        if not raw:
            return None
        try:
            ts = float(raw)
            if ts > 1_000_000_000:
                return datetime.fromtimestamp(ts).date()
        except (ValueError, TypeError, OSError):
            pass
        for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d%b%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    def resolve(self, sig: dict) -> Optional[dict]:
        """Resolve a parsed-instrument dict to a tradable contract.

        sig keys: kind (OPT/FUT/EQ), underlying, strike, option_type,
        exchange_segment. Returns instrument_token, trading_symbol,
        exchange_segment, lot_size, tick_size — or None.
        """
        kind = (sig.get("kind") or "OPT").upper()
        segment = sig.get("exchange_segment") or "bse_fo"
        contracts = self._segments.get(segment, [])
        if not contracts:
            # Pre-Phase-2 state (only legacy SENSEX master loaded): delegate
            if kind == "OPT" and segment == "bse_fo":
                return self.lookup(str(sig.get("strike") or ""), sig.get("option_type") or "")
            return None

        def _out(c: dict) -> dict:
            return {
                "instrument_token": str(c.get("pSymbol", "") or c.get("pInstrumentToken", "") or c.get("instrument_token", "")),
                "trading_symbol": self._sym_of(c),
                "exchange_segment": segment,
                "lot_size": c.get("lLotSize", "") or c.get("pLotSize", "") or c.get("lot_size", ""),
                "tick_size": c.get("dTickSize", "") or c.get("pTickSize", "") or c.get("tick_size", ""),
                "raw": c,
            }

        if kind == "EQ":
            sym = str(sig.get("underlying") or "").upper()
            for c in contracts:
                s = self._sym_of(c)
                if s == sym or s.startswith(sym + "-") or s.startswith(sym + " "):
                    return _out(c)
            # nse_cm master may be keyed differently — also try FO list
            log.warning("No contract found for EQ %s (%s)", sym, segment)
            return None

        und = str(sig.get("underlying") or "").upper()
        cands = [c for c in contracts if self._und_matches(self._sym_of(c), und)]
        if not cands:
            log.warning("No contracts for underlying %s (%s)", und, segment)
            return None

        if kind == "FUT":
            # Futures carry no strike in our signals — nearest expiry match.
            # Only accept non-option rows; NEVER fall back to a CE/PE row
            # (that would buy the wrong instrument with real money).
            expiry = self._nearest_expiry(cands)
            pool = [c for c in cands if not expiry or str(
                c.get("pExpiryDate", "") or c.get("expiry_date", "") or c.get("expiry", "")).strip() == expiry]
            for c in pool:
                if self._opt_of(c) in ("", "F", "FUT", "XX"):
                    return _out(c)
            log.warning("No future contract for %s (%s) — refusing option fallback", und, segment)
            return None

        # OPT: strike + type + expiry (signal hint wins, else nearest).
        try:
            want = int(float(str(sig.get("strike") or "")))
        except (ValueError, TypeError):
            return None
        opt = str(sig.get("option_type") or "").upper()
        cands = [c for c in cands if self._strike_of(c) == want and self._opt_of(c) == opt]
        if not cands:
            log.warning("No contract for %s %s %s (%s)", und, want, opt, segment)
            return None
        hint = self._expiry_hint_to_date(str(sig.get("expiry") or ""))
        if hint is not None:
            from datetime import date as _date
            dated = [(c, self._row_expiry_date(c)) for c in cands]
            # Drop undated rows AND stale past rows (Kotak masters contain
            # ancient placeholder expiries, e.g. 2016 — live bug 2026-10-12).
            dated = [(c, d) for c, d in dated
                     if d is not None and d >= _date.today()]
            for c, d in dated:
                if d == hint:
                    return _out(c)
            # Row dates are unreliable — fall back to the date embedded in the
            # broker symbol itself (NIFTY26O13… = 13-Oct). Only day-exact
            # symbols qualify: a month-only row for a day-specific signal is
            # precisely the wrong-expiry bug above, so it refuses instead.
            for c in cands:
                sd = self._symbol_expiry(self._sym_of(c), und)
                if (sd is not None and sd[2] == hint.day
                        and (sd[0], sd[1]) == (hint.year, hint.month)):
                    return _out(c)
            avail = sorted({d.isoformat() for _, d in dated} |
                           {str(self._symbol_expiry(self._sym_of(c), und)) for c in cands})
            log.warning("Hint %s unmatched (available %s) — refusing wrong-expiry fill",
                        hint, avail)
            return None
        # No usable hint: prefer the nearest-future expiry read from the
        # SYMBOLS (row dates are unreliable), then row dates, then legacy.
        sym_dated = []
        for c in cands:
            sd = self._symbol_expiry(self._sym_of(c), und)
            if sd is None:
                continue
            try:
                if sd[2] is None:
                    # Month-only symbol: judge by month-end (Oct-monthly
                    # expires late-Oct, still future mid-month).
                    import calendar as _cal
                    d = date(sd[0], sd[1], _cal.monthrange(sd[0], sd[1])[1])
                else:
                    d = date(sd[0], sd[1], sd[2])
            except ValueError:
                continue
            sym_dated.append((c, d))
        sym_dated = [(c, d) for c, d in sym_dated if d >= date.today()]
        if sym_dated:
            sym_dated.sort(key=lambda cd: cd[1])
            return _out(sym_dated[0][0])
        expiry = self._nearest_expiry(cands)
        for c in cands:
            if not expiry or str(
                    c.get("pExpiryDate", "") or c.get("expiry_date", "") or c.get("expiry", "")).strip() == expiry:
                return _out(c)
        return _out(cands[0])

    def _save_to_csv(self):
        """Save filtered contracts to CSV for reference/debugging."""
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            if not self._contracts:
                return
            keys = self._contracts[0].keys()
            with open(MASTER_FILE, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(self._contracts)
            log.info(f"Saved {len(self._contracts)} contracts to {MASTER_FILE}")
        except Exception as e:
            log.error(f"Failed to save contract master CSV: {e}")

    def _build_lookup_index(self):
        """Build O(1) lookup dict from _contracts. Called after download or CSV load."""
        self._lookup_index = {}
        for c in self._contracts:
            c_strike = str(
                c.get("dStrikePrice;", "") or c.get("pStrikePrice", "") or
                c.get("strike_price", "") or c.get("dStrikePrice", "") or ""
            ).strip()
            c_option = str(c.get("pOptionType", "") or c.get("option_type", "") or "").upper()
            try:
                c_strike_val = int(float(c_strike)) if c_strike else 0
                # Handle Kotak's strike * 100 convention
                if c_strike_val > 99999:
                    c_strike_val = c_strike_val // 100
                key = (str(c_strike_val), c_option)
                if key not in self._lookup_index:
                    self._lookup_index[key] = c
            except (ValueError, TypeError):
                pass
        log.info(f"Lookup index built: {len(self._lookup_index)} entries")

    def _load_from_csv(self) -> bool:
        """Load contracts from previously saved CSV."""
        if not os.path.exists(MASTER_FILE):
            return False
        try:
            with open(MASTER_FILE, "r") as f:
                reader = csv.DictReader(f)
                self._contracts = list(reader)
            if self._contracts:
                log.info(f"Loaded {len(self._contracts)} contracts from cached CSV")
                self._build_lookup_index()
                return True
        except Exception as e:
            log.error(f"Failed to load cached contract master: {e}")
        return False
