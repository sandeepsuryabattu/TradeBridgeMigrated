"""One-shot patch for the strategy modal (signal_trail fields) — RETIRED.

The steps below were applied to frontend/dashboard.js long ago (verified:
sl-activation-points wiring exists in dashboard.js). This script is kept for
history but is now a safe no-op: it never crashes, never rewrites, and simply
reports that there is nothing to do. Do not revive without updating TARGET.
"""
import os

TARGET = "frontend/dashboard.js"  # was frontend/app.js (never existed)

APPLIED_MARKERS = [
    "sl-activation-points",
    "activationPoints",
]

SKIP_REASON = "already applied"


def main() -> int:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), TARGET)
    if not os.path.exists(path):
        print(f"SKIP: {TARGET} not found — nothing to patch.")
        return 0
    with open(path, encoding="utf-8") as f:
        content = f.read()
    missing = [m for m in APPLIED_MARKERS if m not in content]
    if not missing:
        print(f"SKIP ({SKIP_REASON}): all markers present in {TARGET}.")
        return 0
    print(f"NOTE: {TARGET} lacks markers {missing} — patch steps are stale, refusing to guess.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
