#!/usr/bin/env python3
"""
Bulk Grok Registration
======================
Wraps register.py for batch N-account runs with:
- Progress notifications every 10 accounts (BATCH_DONE)
- Auto-push to FoxRouters per batch
- Final bulk_latest.json output

Usage:
  python3 bulk.py 50                    # register 50 accounts
  python3 bulk.py 500 2>&1 | tee run.log
"""

import sys, os, json, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from register import register, push_to_foxrouter, FOX_KEY, FOX_URL, _BASE_DELAY, _MAX_DELAY, log

import argparse
_parser = argparse.ArgumentParser(description="Bulk Grok Registration")
_parser.add_argument("count", type=int, nargs="?", default=10, help="Number of accounts to register")
_args = _parser.parse_args()
TOTAL = _args.count
BASE = os.path.dirname(os.path.abspath(__file__))

ok = []
fail = 0
consecutive_fails = 0
batch_start = 0

print(f"🚀 Starting bulk registration: {TOTAL} accounts")
print(f"🦊 FoxRouters: auto-push={'ON' if FOX_KEY and FOX_URL else 'OFF'}")
print(f"BATCH_START 0/{TOTAL}")
print(flush=True)

for i in range(TOTAL):
    try:
        r = register()
        if r:
            ok.append(r)
        else:
            fail += 1
    except Exception as e:
        fail += 1
        print(f"    ❌ exception: {e}", flush=True)

    done = len(ok) + fail
    if done % 10 == 0 or done == TOTAL:
        # Build batch for FoxRouters push
        batch = [{
            "email": acc["email"],
            "access_token": acc["access_token"],
            "refresh_token": acc["refresh_token"],
            "id_token": acc.get("id_token", ""),
            "expires_in": 21600,
        } for acc in ok[batch_start:]]
        batch_start = len(ok)

        push_count = push_to_foxrouter(batch)

        print(f"\n{'='*60}")
        print(f"BATCH_DONE {done}/{TOTAL} — ✅ {len(ok)} ok, ❌ {fail} fail, 🦊 batch_push={push_count} total_ok={len(ok)}")
        print(f"{'='*60}\n", flush=True)

    if i < TOTAL - 1:
        if r:
            delay = _BASE_DELAY
            consecutive_fails = 0
        else:
            consecutive_fails += 1
            delay = min(_BASE_DELAY * (2 ** consecutive_fails), _MAX_DELAY)
            log.info(f"Backoff: {delay}s after {consecutive_fails} consecutive failure(s)")
        time.sleep(delay)

# Final summary
print(f"\n{'='*60}")
print(f"ALL_DONE {len(ok)}/{TOTAL} registered, {fail} failed")
print(f"{'='*60}")

# Save bulk JSON
bulk = [{
    "email": r["email"],
    "access_token": r["access_token"],
    "refresh_token": r["refresh_token"],
    "id_token": r.get("id_token", ""),
    "expires_in": 21600,
} for r in ok]

out_path = os.path.join(BASE, "bulk_latest.json")
with open(out_path, "w") as f:
    json.dump(bulk, f, indent=2)

print(f"\n📁 Saved: bulk_latest.json ({len(bulk)} accounts)")
print(flush=True)
