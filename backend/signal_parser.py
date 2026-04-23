"""
Signal Parser — Sensex Options Trade Signal Parser (Regex-based)
Updated for new message format (April 2025+)

New format example:
    RISKY JACKPOT
    SENSEX
    📈📉 78500PE
    📊 PRICE @ 270-280
    STOPLOSS
    260
    🎯 TARGETS
    300/500/650
    23rd APRIL EXPIRY

Changes from original:
  - Rule 1: Header is now optional. Signal is valid if it has SENSEX + PRICE @ structure.
             Header type (RISKY JACKPOT / RISKY TRADER'S / SCALPERS ONLY) captured as signal_type.
  - Rule 5.6: Targets now use slash-separated format (300/500/650), not T1/T2/T3 labels.
  - Rule 5.5: SL handles both inline ("STOPLOSS 250") and next-line ("STOPLOSS\n250").
  - New field: expiry extracted from "23rd APRIL EXPIRY" style line.
"""

import re
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class ParsedSignal:
    status: str                          # 'valid', 'ignored', 'empty'
    reason: Optional[str] = None
    signal_type: Optional[str] = None   # 'RISKY JACKPOT', 'RISKY TRADERS', 'SCALPERS', 'PLAIN'
    index: Optional[str] = None
    strike: Optional[str] = None
    option_type: Optional[str] = None   # CE / PE
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    targets: Optional[list] = None
    diff: Optional[float] = None
    stoploss: Optional[float] = None
    expiry: Optional[str] = None
    average: Optional[float] = None

    def to_dict(self):
        return asdict(self)


def _detect_signal_type(lower: str) -> str:
    """Classify the signal header type."""
    if "risky jackpot" in lower:
        return "RISKY JACKPOT"
    if "risky trader" in lower:
        return "RISKY TRADERS"
    if "scalpers" in lower:
        return "SCALPERS"
    if "trading floor" in lower:
        return "TRADING FLOOR"
    return "PLAIN"


def parse_signal(text: str) -> ParsedSignal:
    """
    Parse a Telegram signal message into a structured trade signal.
    """
    if not text or not text.strip():
        return ParsedSignal(status="empty")

    stripped = text.strip()
    lower = stripped.lower()

    # ── Rule 1: Must contain "sensex" ──
    if "sensex" not in lower:
        return ParsedSignal(status="ignored", reason='Does not contain "sensex"')

    # ── Rule 2: Must contain price indicator ──
    if "price" not in lower:
        return ParsedSignal(status="ignored", reason='Does not contain "price"')

    # ── Rule 3: Strike price — exactly 5 digits + CE/PE ──
    option_match = re.search(r"(\d{5})\s*(CE|PE)", stripped, re.IGNORECASE)
    if not option_match:
        return ParsedSignal(status="ignored", reason="Strike (5 digits) + CE/PE not found")

    strike = option_match.group(1)
    option_type = option_match.group(2).upper()

    # ── Rule 4: Entry price range — after "price" keyword ──
    remaining = stripped[option_match.end():]
    price_kw = re.search(r"price", remaining, re.IGNORECASE)
    if not price_kw:
        return ParsedSignal(status="ignored", reason='"price" keyword not found after strike')

    after_price = remaining[price_kw.end():]
    price_match = re.search(r"@?\s*(\d{1,5})(?:\s*[-]\s*(\d{1,5}))?", after_price)
    if not price_match:
        return ParsedSignal(status="ignored", reason='Price range not found after "price" keyword')

    low = int(price_match.group(1))
    high = int(price_match.group(2)) if price_match.group(2) else low

    # ── Rule 5: Average override ──
    avg_match = re.search(r"(?:average|avg)\s*@?\s*(\d{1,5})", stripped, re.IGNORECASE)
    average = None
    if avg_match:
        average = float(avg_match.group(1))
        low = int(average)

    # ── Rule 5.5: Stoploss — handles both inline and next-line formats ──
    #   "STOPLOSS 250"  OR  "STOPLOSS\n250"
    sl_match = re.search(r"(?:stoploss|sl|stls)[^\d]*(\d{1,5})", stripped, re.IGNORECASE)
    stoploss = float(sl_match.group(1)) if sl_match else None

    # ── Rule 5.6: Targets — slash-separated after TARGETS keyword ──
    #   "🎯 TARGETS\n300/500/650"  OR  "🎯 TARGETS 300/500/650"
    targets = []
    tgt_match = re.search(r"targets?\s*\n?\s*([\d/]+)", stripped, re.IGNORECASE)
    if tgt_match:
        targets = [float(t) for t in tgt_match.group(1).split("/") if t.strip().isdigit()]

    # Fallback: comma-separated targets
    if not targets:
        tgt_match2 = re.search(r"targets?\s*[:\-]?\s*([\d,\s]+)", stripped, re.IGNORECASE)
        if tgt_match2:
            targets = [float(t) for t in re.findall(r"\d+", tgt_match2.group(1))]

    # ── Rule 5.7: Expiry date — "23rd APRIL EXPIRY" ──
    expiry_match = re.search(
        r"(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s+expiry)",
        stripped, re.IGNORECASE
    )
    expiry = expiry_match.group(1).strip() if expiry_match else None

    # ── Rule 6: Validation ──
    if high < low:
        return ParsedSignal(status="ignored", reason="High < Low")
    if abs(high - low) > 50:
        return ParsedSignal(status="ignored", reason="Range difference > 50")

    return ParsedSignal(
        status="valid",
        signal_type=_detect_signal_type(lower),
        index="SENSEX",
        strike=strike,
        option_type=option_type,
        entry_low=float(low),
        entry_high=float(high),
        targets=targets if targets else None,
        diff=float(abs(high - low)),
        stoploss=stoploss,
        expiry=expiry,
        average=average,
    )


def parse_bulk(messages: list) -> list:
    """Parse a list of messages and return results as dicts."""
    results = []
    for msg in messages:
        signal = parse_signal(msg)
        results.append({"original": msg, "result": signal.to_dict()})
    return results


# ── Quick self-test ──
if __name__ == "__main__":
    TEST_MESSAGES = [
        # New format: RISKY JACKPOT, multiline SL, slash targets
        """RISKY JACKPOT
SENSEX
📈📉 78600CE
📊 PRICE @ 530-535
STOPLOSS
525
🎯 TARGETS
585/650/800
23rd APRIL EXPIRY""",

        # New format: RISKY TRADER'S, inline SL
        """RISKY TRADER'S ONLY
🔽🔽🔽
SENSEX
📈📉 78200PE
📊 PRICE @ 237-240
STOPLOSS 235
🎯 TARGETS 280/400/560
23rd APRIL EXPIRY""",

        # New format: bare signal (no header)
        """SENSEX
📈📉 77200PE
📊 PRICE @ 310-320
STOPLOSS
300
🎯 TARGETS
345/400/500
23rd APRIL EXPIRY""",

        # New format: SCALPERS ONLY
        """SCALPERS ONLY
🔽🔽🔽
SENSEX
📈📉 78000PE
📊 PRICE @ 340-345
STOPLOSS
338
🎯 TARGETS
378/430/600
23rd APRIL EXPIRY""",

        # New format: with AVERAGE override
        """RISKY JACKPOT
SENSEX
📈📉 78000PE
📊 PRICE @ 265-275
AVERAGE 258
STOPLOSS
250
🎯 TARGETS
320/400/560
23rd APRIL EXPIRY""",

        # Old format still works
        """Trading Floor :-
SENSEX
📈📉 72800PE
📊 PRICE @ 385-395
STOPLOSS
380
🎯 TARGETS
426/485/560
25th MARCH EXPIRY""",

        # Should be ignored — no SENSEX
        "GOOD MORNING TRADER'S 🌞",

        # Should be ignored — no price
        "SENSEX\n78500PE\nSOME OTHER TEXT",
    ]

    for msg in TEST_MESSAGES:
        result = parse_signal(msg)
        print(f"\n{'='*50}")
        print(f"INPUT : {msg[:60].replace(chr(10), ' | ')}...")
        print(f"STATUS: {result.status}")
        if result.status == "valid":
            print(f"TYPE  : {result.signal_type}")
            print(f"STRIKE: {result.strike}{result.option_type}")
            print(f"ENTRY : {result.entry_low} - {result.entry_high}")
            print(f"SL    : {result.stoploss}")
            print(f"TGT   : {result.targets}")
            print(f"EXPIRY: {result.expiry}")
            if result.average:
                print(f"AVG   : {result.average}")
        else:
            print(f"REASON: {result.reason}")