"""
Fix Upstox holiday API 403 — add User-Agent header to urllib request.
Run from ~/telegram-kotak-trader/ on server.
"""
import os

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backend/market_feed.py')
content = open(path).read()

old = '            with urllib.request.urlopen(url, timeout=10) as resp:'
new = '            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})\n            with urllib.request.urlopen(req, timeout=10) as resp:'

assert old in content, "MATCH FAILED"
content = content.replace(old, new, 1)
open(path, 'w').write(content)
print("✅ Fix — market_feed.py: added User-Agent to Upstox holiday request")
