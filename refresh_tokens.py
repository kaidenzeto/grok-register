#!/usr/bin/env python3
"""
Refresh Grok OAuth Tokens
==========================
Refreshes expired access tokens using refresh_token.
Can refresh single token file or all tokens in a directory.

Usage:
  python3 refresh_tokens.py                    # refresh all in tokens/
  python3 refresh_tokens.py tokens/alex.json   # refresh single file
"""

import requests, json, sys, os, time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(BASE, "config.json")) as f:
    CFG = json.load(f)

CLIENT_ID = CFG.get("client_id", "b1a00492-073a-47ea-816f-4c329264a828")
TOKENS_DIR = CFG.get("tokens_dir", os.path.join(BASE, "tokens"))


def refresh_one(filepath):
    """Refresh a single token file. Returns True if refreshed."""
    with open(filepath) as f:
        data = json.load(f)

    rt = data.get("refresh_token")
    if not rt:
        return False

    try:
        r = requests.post(
            "https://auth.x.ai/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": rt,
                "client_id": CLIENT_ID,
            },
            timeout=15,
        )
        d = r.json()
        if "access_token" in d:
            data["access_token"] = d["access_token"]
            if d.get("refresh_token"):
                data["refresh_token"] = d["refresh_token"]
            if d.get("id_token"):
                data["id_token"] = d["id_token"]
            data["expires_in"] = d.get("expires_in", 21600)
            data["refreshed_at"] = datetime.now(timezone.utc).isoformat()
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
            return True
        else:
            print(f"  ❌ {os.path.basename(filepath)}: {d.get('error', 'unknown')}")
            return False
    except Exception as e:
        print(f"  ❌ {os.path.basename(filepath)}: {e}")
        return False


def main():
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if os.path.isfile(target):
            ok = refresh_one(target)
            print(f"{'✅' if ok else '❌'} {target}")
            return

    # Refresh all in directory
    if not os.path.isdir(TOKENS_DIR):
        print(f"❌ Token directory not found: {TOKENS_DIR}")
        return

    files = sorted(f for f in os.listdir(TOKENS_DIR) if f.endswith(".json"))
    ok, fail = 0, 0
    for fname in files:
        fp = os.path.join(TOKENS_DIR, fname)
        if refresh_one(fp):
            ok += 1
        else:
            fail += 1
        time.sleep(0.5)  # rate limit friendly

    print(f"\n✅ {ok} refreshed, ❌ {fail} failed ({len(files)} total)")


if __name__ == "__main__":
    main()
