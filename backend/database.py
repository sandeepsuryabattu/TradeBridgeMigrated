"""
Database module — SQLite via aiosqlite for async trade/message storage.

Uses singleton connections with WAL mode to prevent "database is locked"
errors under high tick-frequency writes. Each database (trades, ticks)
gets one long-lived connection reused across all callers.
"""
import aiosqlite
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"
TICKS_DB_PATH = Path(__file__).parent.parent / "data" / "ticks.db"

# ── Whitelists for dynamic UPDATE queries ──

_ALLOWED_TRADE_FIELDS = {
    "status", "fill_price", "fill_time", "pnl", "order_id",
    "notes", "trigger_price", "price", "quantity", "min_ltp",
    "exit_price", "entry_label", "closed_at", "exit_reason",
    "kotak_order_id", "entry_timer_mins", "exit_timer_mins",
    "entry_slippage", "exit_slippage", "tune_json",
}

# [FIX #7, #2, #14] expanded to cover all SL state + price-side confirmation fields
_ALLOWED_POSITION_FIELDS = {
    "status", "current_price", "pnl", "max_ltp", "trailing_sl",
    "closed_at", "entry_price",
    # SL config fields (persisted so rehydrate_from_db can restore them)
    "sl_mode", "sl_gap", "sl_points", "signal_stoploss",
    "activation_points", "trail_gap", "sl_activated", "exit_reason",
    # Real trading — Kotak order IDs for SL management
    "kotak_entry_order_id", "sl_order_id",
    # Real trading runtime controls for restart-safe behavior
    "exit_timer_mins", "exit_slippage",
    # Snap-level SL runtime state
    "snap_levels", "snap_level_hit", "snap_trailing_active",
    # Activation/snap config (previously written but rejected by whitelist)
    "activation_sl_offset", "snap_trail_gap", "snap_trail_after_l3",
}

_ALLOWED_PENDING_ORDER_FIELDS = {
    "status", "fill_price", "fill_time", "order_id", "notes",
    "trigger_price", "price", "quantity", "tune_json",
    # [FIX #14] price-side confirmation state
    "price_side_candidate", "price_side_confirm_count",
}

# ── Singleton connection holders ──

_db_conn: aiosqlite.Connection | None = None
_ticks_conn: aiosqlite.Connection | None = None


def _utc_now() -> str:
    """Return current UTC time as an ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _get_db() -> aiosqlite.Connection:
    """Return the singleton trades DB connection (creates on first call)."""
    global _db_conn
    if _db_conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db_conn = await aiosqlite.connect(DB_PATH)
        await _db_conn.execute("PRAGMA journal_mode=WAL")
        await _db_conn.execute("PRAGMA busy_timeout=5000")
        _db_conn.row_factory = aiosqlite.Row
    return _db_conn


async def _get_ticks_db() -> aiosqlite.Connection:
    """Return the singleton ticks DB connection (creates on first call)."""
    global _ticks_conn
    if _ticks_conn is None:
        TICKS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _ticks_conn = await aiosqlite.connect(TICKS_DB_PATH)
        await _ticks_conn.execute("PRAGMA journal_mode=WAL")
        await _ticks_conn.execute("PRAGMA busy_timeout=5000")
    return _ticks_conn


async def close_connections():
    """Close all singleton DB connections (call on shutdown)."""
    global _db_conn, _ticks_conn
    if _db_conn:
        await _db_conn.close()
        _db_conn = None
    if _ticks_conn:
        await _ticks_conn.close()
        _ticks_conn = None


async def init_db():
    """Create tables if they don't exist, then run idempotent migrations."""
    db = await _get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT    NOT NULL DEFAULT 'telegram',
            raw_text    TEXT    NOT NULL,
            sender      TEXT,
            timestamp   TEXT    NOT NULL,
            parsed      INTEGER DEFAULT 0,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id  INTEGER,
            status      TEXT    NOT NULL,
            reason      TEXT,
            idx         TEXT,
            strike      TEXT,
            option_type TEXT,
            entry_low   REAL,
            entry_high  REAL,
            diff        REAL,
            stoploss    REAL,
            targets     TEXT,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (message_id) REFERENCES messages(id)
        );

        CREATE TABLE IF NOT EXISTS trades (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id        INTEGER,
            mode             TEXT    NOT NULL DEFAULT 'paper',
            exchange_segment TEXT,
            trading_symbol   TEXT,
            transaction_type TEXT,
            order_type       TEXT,
            quantity         INTEGER,
            price            REAL,
            trigger_price    REAL,
            status           TEXT    NOT NULL DEFAULT 'pending',
            order_id         TEXT,
            fill_price       REAL,
            fill_time        TEXT,
            pnl              REAL    DEFAULT 0,
            min_ltp          REAL,
            exit_price       REAL,
            notes            TEXT,
            entry_label      TEXT,
            exit_reason      TEXT,
            entry_timer_mins INTEGER,
            exit_timer_mins  INTEGER,
            entry_slippage   REAL,
            exit_slippage    REAL,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        );

        -- [FIX: perf] Index for fast duplicate-signal lookup in trade_manager
        CREATE INDEX IF NOT EXISTS idx_trades_signal_id
            ON trades(signal_id, created_at);

        CREATE TABLE IF NOT EXISTS positions (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id                INTEGER,
            mode                    TEXT    NOT NULL DEFAULT 'paper',
            trading_symbol          TEXT,
            strike                  TEXT,
            option_type             TEXT,
            quantity                INTEGER,
            entry_price             REAL,
            current_price           REAL    DEFAULT 0,
            pnl                     REAL    DEFAULT 0,
            max_ltp                 REAL    DEFAULT 0,
            trailing_sl             REAL    DEFAULT 0,
            status                  TEXT    NOT NULL DEFAULT 'open',
            opened_at               TEXT    NOT NULL DEFAULT (datetime('now')),
            closed_at               TEXT,
            -- [FIX #2] SL config — persisted so rehydrate_from_db restores full state
            sl_mode                 TEXT    DEFAULT 'fixed',
            sl_gap                  REAL    DEFAULT 0,
            sl_points               REAL    DEFAULT 0,
            signal_stoploss         REAL    DEFAULT 0,
            activation_points       REAL    DEFAULT 0,
            trail_gap               REAL    DEFAULT 0,
            -- [FIX #7] Trailing SL runtime state — written on every tick
            sl_activated            INTEGER DEFAULT 0,
            exit_reason             TEXT,
            exit_timer_mins         INTEGER,
            exit_slippage           REAL,
            FOREIGN KEY (trade_id) REFERENCES trades(id)
        );

        -- [FIX #14] Pending orders table with price-side confirmation state
        CREATE TABLE IF NOT EXISTS pending_orders (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id               INTEGER,
            trade_id                INTEGER,
            mode                    TEXT    NOT NULL DEFAULT 'paper',
            trading_symbol          TEXT,
            strike                  TEXT,
            option_type             TEXT,
            quantity                INTEGER,
            price                   REAL,
            trigger_price           REAL,
            status                  TEXT    NOT NULL DEFAULT 'pending',
            order_id                TEXT,
            fill_price              REAL,
            fill_time               TEXT,
            notes                   TEXT,
            -- price-side confirmation state (in-memory previously — now persisted)
            price_side_candidate    TEXT,
            price_side_confirm_count INTEGER DEFAULT 0,
            created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (signal_id) REFERENCES signals(id),
            FOREIGN KEY (trade_id)  REFERENCES trades(id)
        );
    """)
    await db.commit()

    # ── Schema migrations (idempotent — safe to run on every startup) ──
    migrations = [
        # trades table
        ("trades",    "exit_price",               "REAL"),
        ("trades",    "entry_label",              "TEXT"),
        ("trades",    "closed_at",                "TEXT"),
        ("trades",    "exit_reason",              "TEXT"),
        ("signals",   "last_ltp",                 "REAL"),
        ("signals",   "expiry",                   "TEXT"),
        # Phase 4 (MCX): variant + segment persist so resubscribe re-resolves
        # the exact product (mini vs standard) after a restart.
        ("signals",   "variant",                  "TEXT DEFAULT 'standard'"),
        ("signals",   "exchange_segment",         "TEXT DEFAULT 'bse_fo'"),
        # Exact resolved contract per signal — lets LTP backfill skipped
        # signals (no trade row) without cross-expiry bleed.
        ("signals",   "trading_symbol",           "TEXT"),
        # Instrument kind (OPT/FUT/EQ) so cards can badge FUT/EQ rows,
        # which carry no CE/PE option_type.
        ("signals",   "kind",                     "TEXT DEFAULT 'OPT'"),
        # positions table — SL config fields
        ("positions", "sl_mode",                  "TEXT DEFAULT 'fixed'"),
        ("positions", "sl_gap",                   "REAL DEFAULT 0"),
        ("positions", "sl_points",                "REAL DEFAULT 0"),
        ("positions", "signal_stoploss",          "REAL DEFAULT 0"),
        ("positions", "activation_points",        "REAL DEFAULT 0"),
        ("positions", "trail_gap",                "REAL DEFAULT 0"),
        # positions table — trailing SL runtime state
        ("positions", "sl_activated",             "INTEGER DEFAULT 0"),
        ("positions", "exit_reason",              "TEXT"),
        # pending_orders table — price-side confirmation state
        ("pending_orders", "price_side_candidate",     "TEXT"),
        ("pending_orders", "price_side_confirm_count", "INTEGER DEFAULT 0"),
        # Real trading — Kotak order IDs
        ("trades",          "kotak_order_id",           "TEXT"),
        ("trades",          "entry_timer_mins",         "INTEGER"),
        ("trades",          "exit_timer_mins",          "INTEGER"),
        ("trades",          "entry_slippage",           "REAL"),
        ("trades",          "exit_slippage",            "REAL"),
        ("positions",       "kotak_entry_order_id",     "TEXT"),
        ("positions",       "sl_order_id",              "TEXT"),
        ("positions",       "exit_timer_mins",          "INTEGER"),
        ("positions",       "exit_slippage",            "REAL"),
        # Snap-level SL state
        ("positions",       "snap_levels",              "TEXT"),
        ("positions",       "snap_level_hit",           "INTEGER DEFAULT 0"),
        ("positions",       "snap_trailing_active",     "INTEGER DEFAULT 0"),
        # Activation/snap config — previously lost on rehydrate / rejected writes
        ("positions",       "activation_sl_offset",     "REAL DEFAULT 0"),
        ("positions",       "snap_trail_gap",           "REAL DEFAULT 3.0"),
        ("positions",       "snap_trail_after_l3",      "INTEGER DEFAULT 0"),
        # Phase 3: segment persists so post-restart exits use the right exchange
        ("positions",       "exchange_segment",         "TEXT DEFAULT 'bse_fo'"),
        # Per-trade tune snapshot (order_tune.py) — survives restart via rehydrate
        ("trades",          "tune_json",                "TEXT"),
        ("pending_orders",  "tune_json",                "TEXT"),
        # Autotrender model attribution — which model version/score produced
        # the signal (set by backend/autotrender.py on the paper path only).
        ("signals",         "model_version",            "TEXT"),
        ("signals",         "model_score",              "REAL"),
    ]

    for table, col, typedef in migrations:
        try:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
            await db.commit()
            log.info("Migration: added column '%s' to table '%s'", col, table)
        except aiosqlite.OperationalError as e:
            if "duplicate column" in str(e).lower():
                pass  # already exists — expected on every startup after first run
            else:
                log.warning("Migration warning for %s.%s: %s", table, col, e)

    ticks_db = await _get_ticks_db()
    await ticks_db.executescript("""
        CREATE TABLE IF NOT EXISTS ticks (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            instrument_token TEXT,
            symbol           TEXT,
            ltp              REAL    NOT NULL,
            volume           INTEGER DEFAULT 0,
            open             REAL    DEFAULT 0,
            high             REAL    DEFAULT 0,
            low              REAL    DEFAULT 0,
            close            REAL    DEFAULT 0,
            timestamp        TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """)
    await ticks_db.commit()


async def clear_all_data(date: str = None):
    """Clear dashboard data from the database.

    date=None        → full wipe of all tables + resets auto-increment
                       sequences. Also clears the ticks DB.
    date='YYYY-MM-DD' → deletes only records whose IST date matches.
                       Uses +5:30 offset consistent with get_signals/get_trades.
                       Open positions are never deleted — only closed ones
                       whose closed_at IST date matches are removed.
                       Pending orders for affected signals are also removed.
                       Ticks DB is left untouched on date-specific clears.
    """
    db = await _get_db()

    if date is None:
        # ── Full wipe ──────────────────────────────────────────────
        await db.execute("DELETE FROM pending_orders")
        await db.execute("DELETE FROM positions")
        await db.execute("DELETE FROM trades")
        await db.execute("DELETE FROM signals")
        await db.execute("DELETE FROM messages")
        await db.execute(
            "DELETE FROM sqlite_sequence WHERE name IN "
            "('pending_orders', 'positions', 'trades', 'signals', 'messages')"
        )
        await db.commit()

        ticks_db = await _get_ticks_db()
        await ticks_db.execute("DELETE FROM ticks")
        await ticks_db.execute("DELETE FROM sqlite_sequence WHERE name = 'ticks'")
        await ticks_db.commit()

    else:
        # ── Date-specific wipe (IST, same offset as get_trades/get_signals) ──
        IST = "'+5 hours', '+30 minutes'"

        # Pending orders whose parent signal was created on this IST date
        await db.execute(
            f"""DELETE FROM pending_orders WHERE signal_id IN (
                    SELECT id FROM signals
                    WHERE date(datetime(created_at, {IST})) = date(?)
                )""",
            (date,),
        )

        # Closed positions whose closed_at IST date matches
        # (never delete open positions — they are still active)
        await db.execute(
            f"""DELETE FROM positions
                WHERE status = 'closed'
                AND date(datetime(closed_at, {IST})) = date(?)""",
            (date,),
        )

        # Trades created on this IST date
        await db.execute(
            f"""DELETE FROM trades
                WHERE date(datetime(created_at, {IST})) = date(?)""",
            (date,),
        )

        # Signals created on this IST date
        await db.execute(
            f"""DELETE FROM signals
                WHERE date(datetime(created_at, {IST})) = date(?)""",
            (date,),
        )

        # Messages — uses 'timestamp' column (not created_at)
        await db.execute(
            f"""DELETE FROM messages
                WHERE date(datetime(timestamp, {IST})) = date(?)""",
            (date,),
        )

        await db.commit()
        log.info("Date-specific clear complete for IST date: %s", date)


# ── Message CRUD ──

async def save_message(raw_text: str, sender: str = "", source: str = "telegram") -> int:
    db = await _get_db()
    cursor = await db.execute(
        "INSERT INTO messages (source, raw_text, sender, timestamp) VALUES (?, ?, ?, ?)",
        (source, raw_text, sender, _utc_now()),
    )
    await db.commit()
    return cursor.lastrowid


async def get_messages(limit: int = 100, date: str = None) -> list[dict]:
    """Fetch messages, optionally filtered by date (YYYY-MM-DD)."""
    db = await _get_db()
    if date:
        cursor = await db.execute(
            "SELECT * FROM messages WHERE date(datetime(timestamp, '+5 hours', '+30 minutes')) = date(?) ORDER BY id DESC LIMIT ?",
            (date, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ── Signal CRUD ──

async def save_signal(message_id: int, parsed: dict) -> int:
    targets_json = json.dumps(parsed["targets"]) if parsed.get("targets") else None
    db = await _get_db()
    cursor = await db.execute(
        """INSERT INTO signals
           (message_id, status, reason, idx, strike, option_type,
            entry_low, entry_high, diff, stoploss, targets, expiry,
            variant, exchange_segment, kind)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            parsed.get("status"),
            parsed.get("reason"),
            parsed.get("index"),
            parsed.get("strike"),
            parsed.get("option_type"),
            parsed.get("entry_low"),
            parsed.get("entry_high"),
            parsed.get("diff"),
            parsed.get("stoploss"),
            targets_json,
            parsed.get("expiry"),
            parsed.get("variant") or "standard",
            parsed.get("exchange_segment") or "bse_fo",
            parsed.get("kind") or "OPT",
        ),
    )
    await db.execute(
        "UPDATE messages SET parsed = 1 WHERE id = ?", (message_id,)
    )
    await db.commit()
    return cursor.lastrowid


_ALLOWED_SIGNAL_FIELDS = {
    "status", "reason", "last_ltp", "trading_symbol", "variant",
    "exchange_segment", "expiry", "model_version", "model_score",
}


async def update_signal(signal_id: int, updates: dict):
    invalid = set(updates.keys()) - _ALLOWED_SIGNAL_FIELDS
    if invalid:
        raise ValueError(f"update_signal: disallowed fields: {invalid}")
    if not updates:
        return
    db = await _get_db()
    set_clauses = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [signal_id]
    await db.execute(
        f"UPDATE signals SET {set_clauses} WHERE id = ?", values
    )
    await db.commit()


async def get_signals(limit: int = 100, date: str = None) -> list[dict]:
    """Fetch signals, optionally filtered by date (YYYY-MM-DD)."""
    db = await _get_db()
    if date:
        cursor = await db.execute(
            "SELECT * FROM signals WHERE date(datetime(created_at, '+5 hours', '+30 minutes')) = date(?) ORDER BY id DESC LIMIT ?",
            (date, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
        )
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("targets"):
            try:
                d["targets"] = json.loads(d["targets"])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(d)
    return result


# ── Trade CRUD ──

async def save_trade(signal_id: int, trade_data: dict) -> int:
    db = await _get_db()
    cursor = await db.execute(
        """INSERT INTO trades
           (signal_id, mode, exchange_segment, trading_symbol, transaction_type,
            order_type, quantity, price, trigger_price, status, order_id, min_ltp,
            "notes", "entry_label", "exit_reason",
            "entry_timer_mins", "exit_timer_mins", "entry_slippage", "exit_slippage",
            "tune_json"
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            signal_id,
            trade_data.get("mode", "paper"),
            trade_data.get("exchange_segment"),
            trade_data.get("trading_symbol"),
            trade_data.get("transaction_type", "B"),
            trade_data.get("order_type", "L"),
            trade_data.get("quantity"),
            trade_data.get("price"),
            trade_data.get("trigger_price", 0),
            trade_data.get("status", "pending"),
            trade_data.get("order_id"),
            trade_data.get("min_ltp"),
            trade_data.get("notes"),
            trade_data.get("entry_label"),
            trade_data.get("exit_reason"),
            trade_data.get("entry_timer_mins"),
            trade_data.get("exit_timer_mins"),
            trade_data.get("entry_slippage"),
            trade_data.get("exit_slippage"),
            trade_data.get("tune_json"),
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def update_trade(trade_id: int, updates: dict):
    invalid = set(updates.keys()) - _ALLOWED_TRADE_FIELDS
    if invalid:
        raise ValueError(f"update_trade: disallowed fields: {invalid}")
    if not updates:
        return
    db = await _get_db()
    set_clauses = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [trade_id]
    await db.execute(f"UPDATE trades SET {set_clauses} WHERE id = ?", values)
    await db.commit()


async def get_trades(mode: str = None, limit: int = 200, date: str = None) -> list[dict]:
    """Fetch trades, optionally filtered by mode and/or date (YYYY-MM-DD)."""
    db = await _get_db()
    conditions = []
    params = []
    if mode:
        conditions.append("mode = ?")
        params.append(mode)
    if date:
        conditions.append("date(datetime(created_at, '+5 hours', '+30 minutes')) = date(?)")
        params.append(date)
    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)
    cursor = await db.execute(
        f"SELECT * FROM trades WHERE {where} ORDER BY id DESC LIMIT ?", params
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


def compute_perf(rows: list[dict]) -> dict:
    """Pure stats over closed-trade rows {pnl, ist_date, sender, model_version}.

    Returns overall + by_day + by_source + by_model with win-rate, profit
    factor, expectancy and a day-ordered equity curve for drawdown.
    """
    def blank():
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "pnl": 0.0,
                "gross_win": 0.0, "gross_loss": 0.0, "profit_factor": 0.0,
                "expectancy": 0.0, "best": 0.0, "worst": 0.0}

    def fill(b, ps):
        b["trades"] = len(ps)
        if not ps:
            return b
        b["wins"] = sum(1 for p in ps if p > 0)
        b["win_rate"] = round(100.0 * b["wins"] / len(ps), 1)
        b["pnl"] = round(sum(ps), 2)
        b["gross_win"] = round(sum(p for p in ps if p > 0), 2)
        b["gross_loss"] = round(sum(-p for p in ps if p < 0), 2)
        b["profit_factor"] = round(b["gross_win"] / b["gross_loss"], 2) if b["gross_loss"] else (
            float("inf") if b["gross_win"] else 0.0)
        b["expectancy"] = round(sum(ps) / len(ps), 2)
        b["best"] = round(max(ps), 2)
        b["worst"] = round(min(ps), 2)
        return b

    overall = fill(blank(), [float(r.get("pnl") or 0) for r in rows])
    by_day, by_source, by_model = {}, {}, {}
    for r in rows:
        p = float(r.get("pnl") or 0)
        by_day.setdefault(str(r.get("ist_date") or "?"), []).append(p)
        by_source.setdefault(str(r.get("sender") or "Unknown"), []).append(p)
        mv = r.get("model_version")
        if mv:
            by_model.setdefault(str(mv), []).append(p)
    eq, peak, maxdd = 0.0, 0.0, 0.0
    for day in sorted(by_day):
        eq += sum(by_day[day])
        peak = max(peak, eq)
        maxdd = min(maxdd, eq - peak)
    return {
        "overall": overall,
        "by_day": {d: fill(blank(), ps) for d, ps in sorted(by_day.items())},
        "by_source": {s: fill(blank(), ps) for s, ps in sorted(by_source.items())},
        "by_model": {m: fill(blank(), ps) for m, ps in sorted(by_model.items())},
        "max_drawdown": round(maxdd, 2),
        "equity_curve": [{"day": d, "equity": round(sum(sum(by_day[x]) for x in sorted(by_day) if x <= d), 2)}
                         for d in sorted(by_day)],
    }


async def get_performance(mode: str = "paper", days: int = 30) -> dict:
    """Closed-trade performance, IST trading days, newest last `days` days.

    Source = message sender (Telegram group vs Autotrender); model from
    signals.model_version set on the autotrender paper path.
    """
    db = await _get_db()
    cursor = await db.execute(
        """SELECT t.pnl AS pnl,
                  date(datetime(t.closed_at, '+5 hours', '+30 minutes')) AS ist_date,
                  COALESCE(m.sender, 'Unknown') AS sender,
                  s.model_version AS model_version
           FROM trades t
           LEFT JOIN signals s ON s.id = t.signal_id
           LEFT JOIN messages m ON m.id = s.message_id
           WHERE t.mode = ? AND t.status = 'closed'
             AND date(datetime(t.closed_at, '+5 hours', '+30 minutes'))
                 >= date(datetime('now', '+5 hours', '+30 minutes'), ?)""",
        (mode, f"-{max(1, days) - 1} days"),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    out = compute_perf(rows)
    out.update({"mode": mode, "days": days, "closed_trades": len(rows)})
    return out


# ── Position CRUD ──

async def save_position(trade_id: int, pos_data: dict) -> int:
    db = await _get_db()
    snap_levels_json = json.dumps(pos_data["snap_levels"]) if pos_data.get("snap_levels") else None
    cursor = await db.execute(
        """INSERT INTO positions
           (trade_id, mode, trading_symbol, strike, option_type,
            quantity, entry_price, max_ltp, trailing_sl, status,
            sl_mode, sl_gap, sl_points, signal_stoploss,
            activation_points, trail_gap, sl_activated, exit_reason,
            exit_timer_mins, exit_slippage,
            snap_levels, snap_level_hit, snap_trailing_active,
            activation_sl_offset, snap_trail_gap, snap_trail_after_l3,
            exchange_segment)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            trade_id,
            pos_data.get("mode", "paper"),
            pos_data.get("trading_symbol"),
            pos_data.get("strike"),
            pos_data.get("option_type"),
            pos_data.get("quantity"),
            pos_data.get("entry_price"),
            pos_data.get("max_ltp", pos_data.get("entry_price", 0)),
            pos_data.get("trailing_sl", 0),
            "open",
            pos_data.get("sl_mode", "fixed"),
            pos_data.get("sl_gap", 0),
            pos_data.get("sl_points", 0),
            pos_data.get("signal_stoploss", 0),
            pos_data.get("activation_points", 0),
            pos_data.get("trail_gap", 0),
            int(pos_data.get("sl_activated", False)),
            pos_data.get("exit_reason"),
            pos_data.get("exit_timer_mins"),
            pos_data.get("exit_slippage"),
            snap_levels_json,
            pos_data.get("snap_level_hit", 0),
            int(pos_data.get("snap_trailing_active", False)),
            pos_data.get("activation_sl_offset", 0),
            pos_data.get("snap_trail_gap", 3.0),
            int(pos_data.get("snap_trail_after_l3", False)),
            pos_data.get("exchange_segment", "bse_fo"),
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def update_position(position_id: int, updates: dict):
    invalid = set(updates.keys()) - _ALLOWED_POSITION_FIELDS
    if invalid:
        raise ValueError(f"update_position: disallowed fields: {invalid}")
    if not updates:
        return
    db = await _get_db()
    set_clauses = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [position_id]
    await db.execute(
        f"UPDATE positions SET {set_clauses} WHERE id = ?", values
    )
    await db.commit()


async def get_positions(mode: str = None, status: str = "open", date: str = None) -> list[dict]:
    """
    Fetch positions filtered by mode and/or status.
    status=None returns open positions + closed from the given date (or today if no date).
    date param: 'YYYY-MM-DD' filters closed positions to that day; None = today (IST).
    """
    db = await _get_db()
    conditions: list[str] = []
    params: list = []
    if mode:
        conditions.append("mode = ?")
        params.append(mode)
    if status is None:
        if date:
            conditions.append(
                "(status = 'open' OR (status = 'closed' AND "
                "date(datetime(closed_at, '+5 hours', '+30 minutes')) = ?))"
            )
            params.append(date)
        else:
            # Default: open positions + today's closed (IST date)
            conditions.append(
                "(status = 'open' OR (status = 'closed' AND "
                "date(datetime(closed_at, '+5 hours', '+30 minutes')) = "
                "date(datetime('now', '+5 hours', '+30 minutes'))))"
            )
    else:
        conditions.append("status = ?")
        params.append(status)

    where = " AND ".join(conditions) if conditions else "1=1"
    cursor = await db.execute(
        f"SELECT * FROM positions WHERE {where} ORDER BY id DESC", params
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ── Pending Order CRUD ──  [FIX #14]

async def save_pending_order(signal_id: int, trade_id: int, order_data: dict) -> int:
    db = await _get_db()
    cursor = await db.execute(
        """INSERT INTO pending_orders
           (signal_id, trade_id, mode, trading_symbol, strike, option_type,
            quantity, price, trigger_price, status, order_id, notes,
            price_side_candidate, price_side_confirm_count, tune_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            signal_id,
            trade_id,
            order_data.get("mode", "paper"),
            order_data.get("trading_symbol"),
            order_data.get("strike"),
            order_data.get("option_type"),
            order_data.get("quantity"),
            order_data.get("price"),
            order_data.get("trigger_price", 0),
            order_data.get("status", "pending"),
            order_data.get("order_id"),
            order_data.get("notes"),
            order_data.get("price_side_candidate"),
            order_data.get("price_side_confirm_count", 0),
            order_data.get("tune_json"),
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def update_pending_order(order_id: int, updates: dict):
    invalid = set(updates.keys()) - _ALLOWED_PENDING_ORDER_FIELDS
    if invalid:
        raise ValueError(f"update_pending_order: disallowed fields: {invalid}")
    if not updates:
        return
    db = await _get_db()
    set_clauses = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [order_id]
    await db.execute(
        f"UPDATE pending_orders SET {set_clauses} WHERE id = ?", values
    )
    await db.commit()


async def get_pending_orders(mode: str = None, status: str = "pending") -> list[dict]:
    """Fetch pending orders, optionally filtered by mode and/or status."""
    db = await _get_db()
    conditions: list[str] = []
    params: list = []
    if mode:
        conditions.append("mode = ?")
        params.append(mode)
    if status is not None:
        conditions.append("status = ?")
        params.append(status)
    where = " AND ".join(conditions) if conditions else "1=1"
    cursor = await db.execute(
        f"SELECT * FROM pending_orders WHERE {where} ORDER BY id DESC", params
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def delete_pending_order(order_id: int):
    """Remove a pending order once it is filled, expired, or cancelled."""
    db = await _get_db()
    await db.execute("DELETE FROM pending_orders WHERE id = ?", (order_id,))
    await db.commit()


async def delete_orphan_pending_orders() -> int:
    """Delete pending_orders rows whose trade already left 'pending'.

    Crashes between fill/expiry and row-delete leave orphans that rehydrate
    would otherwise reload as live orders on every boot. Only touches rows
    whose trade is verifiably non-pending (or missing) — never live ones.
    Returns the number of rows deleted.
    """
    db = await _get_db()
    cursor = await db.execute(
        """DELETE FROM pending_orders
           WHERE status = 'pending'
             AND trade_id NOT IN (SELECT id FROM trades WHERE status = 'pending')"""
    )
    await db.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0


# ── Tick Storage (for backtesting) ──

async def save_tick(tick_data: dict):
    db = await _get_ticks_db()
    await db.execute(
        """INSERT INTO ticks
           (instrument_token, symbol, ltp, volume, open, high, low, close, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tick_data.get("instrument_token", ""),
            tick_data.get("symbol", ""),
            tick_data.get("ltp", 0),
            tick_data.get("volume", 0),
            tick_data.get("open", 0),
            tick_data.get("high", 0),
            tick_data.get("low", 0),
            tick_data.get("close", 0),
            tick_data.get("timestamp", _utc_now()),
        ),
    )
    await db.commit()


async def save_ticks_batch(ticks: list[dict]):
    """Save a batch of ticks efficiently in a single transaction."""
    if not ticks:
        return
    db = await _get_ticks_db()
    await db.executemany(
        """INSERT INTO ticks
           (instrument_token, symbol, ltp, volume, open, high, low, close, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                t.get("instrument_token", ""),
                t.get("symbol", ""),
                t.get("ltp", 0),
                t.get("volume", 0),
                t.get("open", 0),
                t.get("high", 0),
                t.get("low", 0),
                t.get("close", 0),
                t.get("timestamp", _utc_now()),
            )
            for t in ticks
        ],
    )
    await db.commit()


async def update_signal_ltp(trading_symbol: str, ltp: float):
    """Update last_ltp on the signal card(s) for this exact contract.

    Scoped by exact trading_symbol — not by strike suffix. Two expiries
    of one strike (NIFTY26O13… vs NIFTY26OCT…) used to overwrite each
    other's cards (live bug: weekly showed the monthly LTP).

    Covers skipped signals too (no trade row): their card still tracks
    the contract while ticks flow.
    """
    db = await _get_db()
    await db.execute(
        """UPDATE signals SET last_ltp = ?
           WHERE status = 'valid' AND (
               trading_symbol = ?
               OR id IN (SELECT signal_id FROM trades WHERE trading_symbol = ?)
           )""",
        (ltp, trading_symbol, trading_symbol),
    )
    await db.commit()
