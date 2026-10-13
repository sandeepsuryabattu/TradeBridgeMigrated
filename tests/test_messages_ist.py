"""Regression: messages filtered by IST trading day, not UTC day.

A message saved at 19:41 UTC (= 01:11 IST next day) must appear under the
IST date. Uses :memory: SQLite — never touches the real trades.db.
"""
import sqlite3

FIXED_Q = ("SELECT * FROM messages WHERE date(datetime(timestamp, "
           "'+5 hours', '+30 minutes')) = date(?) ORDER BY id DESC LIMIT ?")
OLD_Q = "SELECT * FROM messages WHERE date(timestamp) = date(?) ORDER BY id DESC LIMIT ?"


def _db():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, timestamp TEXT)")
    # 2026-10-13 19:41 UTC == 2026-10-14 01:11 IST (the missed CRUDEOIL case)
    c.execute("INSERT INTO messages (timestamp) VALUES ('2026-10-13 19:41:23')")
    # ISO/Zulu shape as written by save_message
    c.execute("INSERT INTO messages (timestamp) VALUES ('2026-10-13T19:41:23.258007Z')")
    c.commit()
    return c


def test_ist_filter_finds_late_night_message():
    c = _db()
    rows = c.execute(FIXED_Q, ("2026-10-14", 100)).fetchall()
    assert len(rows) == 2
    rows_utc_day = c.execute(FIXED_Q, ("2026-10-13", 100)).fetchall()
    assert rows_utc_day == []


def test_old_query_missed_it():
    c = _db()
    assert c.execute(OLD_Q, ("2026-10-14", 100)).fetchall() == []
