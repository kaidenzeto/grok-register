#!/usr/bin/env python3
"""
Grok AI Account Registration
=============================
Pure HTTP registration via xAI gRPC-Web API + CloakBrowser OAuth approval.

Flow:
  1. Solve Cloudflare Turnstile (Boterdrop free → Capsolver paid)
  2. Send OTP via gRPC-Web
  3. Poll OTP from Cloudflare D1 email routing
  4. Register account via gRPC-Web CreateUserAndSession
  5. Create OAuth device code
  6. Approve via headless CloakBrowser
  7. Poll device token (access + refresh + id)

Requirements:
  pip install requests cloakbrowser

Usage:
  python3 register.py                           # 1 random account
  python3 register.py -n 5                      # 5 random accounts
  python3 register.py --email x@domain.com --password 'P@ss'
"""

import requests, json, base64, time, uuid, re, sys, os, random, argparse, logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("grok-register")

BASE = os.path.dirname(os.path.abspath(__file__))


def _load_config():
    """Load and validate config.json with helpful error messages."""
    config_path = os.path.join(BASE, "config.json")
    if not os.path.exists(config_path):
        log.error(f"config.json not found at {config_path}")
        log.error("Copy config.example.json → config.json and fill in your values:")
        log.error("  cp config.example.json config.json")
        sys.exit(1)
    with open(config_path) as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            log.error(f"config.json is not valid JSON: {e}")
            sys.exit(1)
    # Validate required fields
    required = {"d1": ["url", "token"], "email_domain": []}
    for key, subkeys in required.items():
        if key not in cfg:
            log.error(f"Missing required config key: '{key}'. See config.example.json")
            sys.exit(1)
        for sk in subkeys:
            if sk not in cfg[key]:
                log.error(f"Missing required config: '{key}.{sk}'. See config.example.json")
                sys.exit(1)
    if not cfg.get("default_password"):
        log.warning("No default_password set — you must provide --password for every run")
    return cfg


CFG = _load_config()

D1_URL        = CFG["d1"]["url"]
D1_TOKEN      = CFG["d1"]["token"]
CAPSOLVER_KEY = CFG.get("capsolver_key", "")
BOTERDROP_URL = CFG.get("boterdrop_url", "http://127.0.0.1:8005")
CLIENT_ID     = CFG.get("client_id", "b1a00492-073a-47ea-816f-4c329264a828")
SITEKEY       = CFG.get("sitekey", "0x4AAAAAAAhr9JGVDZbrZOo0")
DOMAIN        = CFG.get("email_domain", "yourdomain.com")
DEF_PASS      = CFG.get("default_password", "")
TOKENS_DIR    = CFG.get("tokens_dir", os.path.join(BASE, "tokens"))
ACCTS_FILE    = CFG.get("accounts_file", os.path.join(BASE, "accounts.jsonl"))
FOX_CFG       = CFG.get("foxrouter", {})
FOX_URL       = FOX_CFG.get("url", "")
FOX_KEY       = FOX_CFG.get("key", "")

SCOPE = "openid profile email offline_access grok-cli:access api:access conversations:read conversations:write"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"


# ─── Protobuf helpers ─────────────────────────────────────────

def _ev(v):
    b = []
    while v > 0x7F:
        b.append((v & 0x7F) | 0x80)
        v >>= 7
    b.append(v & 0x7F)
    return b

def _es(fn, val):
    enc = val.encode()
    return _ev((fn << 3) | 2) + _ev(len(enc)) + list(enc)

def _ei(fn, val):
    return _ev((fn << 3) | 0) + _ev(val)

def _em(fn, data):
    if isinstance(data, bytes):
        data = list(data)
    return _ev((fn << 3) | 2) + _ev(len(data)) + data

def _grpc(endpoint, payload, session):
    if isinstance(payload, list):
        payload = bytes(payload)
    frame = bytes([0]) + len(payload).to_bytes(4, "big") + payload
    return session.post(
        f"https://accounts.x.ai/auth_mgmt.AuthManagement/{endpoint}",
        headers={
            "Content-Type": "application/grpc-web-text+proto",
            "X-User-Agent": "grpc-web-javascript/0.1",
            "X-Grpc-Web": "1",
            "User-Agent": UA,
            "Origin": "https://accounts.x.ai",
            "Referer": "https://accounts.x.ai/",
        },
        data=base64.b64encode(frame).decode(),
        timeout=30,
    )


# ─── Turnstile solver ─────────────────────────────────────────

def solve_turnstile():
    """2-tier Turnstile solver: Boterdrop (free) → Capsolver (paid)."""
    t0 = time.time()

    # Tier 1: Boterdrop (free, requires camoufox server on :8005)
    if BOTERDROP_URL:
        try:
            print("    [ts] boterdrop...", end="", flush=True)
            r = requests.get(
                f"{BOTERDROP_URL}/turnstile",
                params={"url": "https://accounts.x.ai/", "sitekey": SITEKEY},
                timeout=15,
            )
            d = r.json()
            task_id = d.get("task_id")
            if task_id:
                for _ in range(30):
                    time.sleep(2)
                    r2 = requests.get(f"{BOTERDROP_URL}/result?id={task_id}", timeout=15)
                    d2 = r2.json()
                    if d2.get("status") == "success" and d2.get("value"):
                        tok = d2["value"]
                        print(f" ✅ ({time.time()-t0:.0f}s)", flush=True)
                        return tok
                    if d2.get("status") in ("error", "failed"):
                        break
                print(" timeout", flush=True)
            else:
                print(f" {d}", flush=True)
        except requests.ConnectionError:
            print(f" ❌ connection refused (is Boterdrop running on {BOTERDROP_URL}?)", flush=True)
        except Exception as e:
            log.debug(f"Boterdrop error: {e}")
            print(f" ❌ {e}", flush=True)

    # Tier 2: Capsolver (paid)
    if CAPSOLVER_KEY:
        try:
            print("    [ts] capsolver...", end="", flush=True)
            r = requests.post(
                "https://api.capsolver.com/createTask",
                json={
                    "clientKey": CAPSOLVER_KEY,
                    "task": {
                        "type": "AntiTurnstileTaskProxyLess",
                        "websiteURL": "https://accounts.x.ai/",
                        "websiteKey": SITEKEY,
                        "metadata": {"action": "managed"},
                    },
                },
                timeout=30,
            )
            resp = r.json()
            tid = resp.get("taskId")
            if tid:
                for _ in range(30):
                    time.sleep(2)
                    r = requests.post(
                        "https://api.capsolver.com/getTaskResult",
                        json={"clientKey": CAPSOLVER_KEY, "taskId": tid},
                        timeout=30,
                    )
                    result = r.json()
                    if result.get("status") == "ready":
                        tok = result["solution"]["token"]
                        print(f" ✅ ({time.time()-t0:.0f}s)", flush=True)
                        return tok
                print(" timeout", flush=True)
            else:
                print(f" ❌ {resp.get('errorDescription', resp)}", flush=True)
        except Exception as e:
            log.debug(f"Capsolver error: {e}")
            print(f" ❌ {e}", flush=True)

    return None


# ─── OTP polling (Cloudflare D1) ──────────────────────────────

def poll_otp(email, wait=90):
    """Poll Cloudflare D1 for OTP code. Requires catch-all email routing."""
    headers = {"Authorization": f"Bearer {D1_TOKEN}", "Content-Type": "application/json"}
    seen = set()
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            r = requests.post(
                D1_URL,
                headers=headers,
                json={
                    "sql": "SELECT subject, create_time FROM email WHERE to_email = ? ORDER BY email_id DESC LIMIT 5",
                    "params": [email],
                },
                timeout=10,
            )
            for row in r.json().get("result", [{}])[0].get("results", []):
                m = re.search(r"\b([A-Z0-9]{2,4}-[A-Z0-9]{2,4})\b", row.get("subject", ""))
                if m:
                    k = f"{m.group(1)}_{row.get('create_time', '')}"
                    if k not in seen:
                        seen.add(k)
                        return m.group(1)
        except requests.ConnectionError:
            log.debug("D1 connection error — retrying")
        except (KeyError, IndexError) as e:
            log.debug(f"D1 response parse error: {e}")
        except Exception as e:
            log.debug(f"OTP poll error: {e}")
        time.sleep(2)
    return None


# ─── Device code helpers ──────────────────────────────────────

def fetch_device_code(retries=3):
    """Get OAuth device code with retries."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                "https://auth.x.ai/oauth2/device/code",
                data={"client_id": CLIENT_ID, "scope": SCOPE},
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"    [device] try {attempt}/{retries}: HTTP {resp.status_code}")
                time.sleep(1.5 * attempt)
                continue
            dr = resp.json()
            if "user_code" in dr and "device_code" in dr:
                return dr
        except Exception as e:
            print(f"    [device] try {attempt}/{retries}: {e}")
            time.sleep(1.5 * attempt)
    return None


def poll_device_token(device_code, max_wait=90):
    """Poll for OAuth token after device approval."""
    t0 = time.time()
    for _ in range(max_wait):
        time.sleep(1)
        try:
            resp = requests.post(
                "https://auth.x.ai/oauth2/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": CLIENT_ID,
                },
                timeout=10,
            )
            d = resp.json()
            if "access_token" in d:
                return d["access_token"], d.get("refresh_token", ""), d.get("id_token", "")
            err = d.get("error")
            if err not in (None, "authorization_pending", "slow_down"):
                print(f"    ❌ token: {err} {d.get('error_description', '')[:80]}")
                return None, None, None
            if err == "slow_down":
                time.sleep(2)
        except Exception as e:
            log.debug(f"Token poll error: {e}")
    print(f"    ❌ token timeout ({max_wait}s)")
    return None, None, None


# ─── Email generator ──────────────────────────────────────────

_NAMES = [
    "alex", "sam", "jordan", "taylor", "morgan", "kai", "aria", "nova", "luna", "milo",
    "leo", "iris", "ruby", "jade", "max", "nora", "emma", "liam", "noah", "ethan",
    "owen", "ella", "chloe", "mason", "lucas", "sofia", "maya", "zoe", "ivy", "cole",
    "luke", "grace", "oliver", "elijah", "theo", "oscar", "felix", "marcus", "sean",
]
_ADJ = [
    "swift", "bright", "calm", "deep", "fast", "bold", "cool", "wild", "keen", "soft",
    "warm", "pure", "vast", "wise", "true", "gold", "slate", "amber", "storm", "frost",
    "ember", "dawn", "dusk", "solar", "neon", "glow", "echo", "apex", "core", "flux",
    "wave", "iron", "onyx", "opal", "pearl", "ocean", "cloud", "mist", "vale", "peak",
]


def rand_email():
    """Generate a random human-looking email address."""
    s = random.randint(0, 2)
    if s == 0:
        return f"{random.choice(_NAMES)}.{random.choice(_NAMES)}{random.randint(1,99)}@{DOMAIN}"
    if s == 1:
        return f"{random.choice(_ADJ)}.{random.choice(_NAMES)}{random.randint(1,99)}@{DOMAIN}"
    return f"{random.choice(_NAMES)}_{random.choice(_ADJ)}{random.randint(1,99)}@{DOMAIN}"


def dec_jwt(token):
    try:
        payload = token.split(".")[1] + "==="
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# ─── FoxRouters push ──────────────────────────────────────────

FOX_HEADERS = {"Content-Type": "application/json"}
if FOX_KEY:
    FOX_HEADERS["Authorization"] = f"Bearer {FOX_KEY}"


def push_to_foxrouter(accounts):
    """Push accounts to FoxRouters /accounts/import/bulk (optional)."""
    if not accounts or not FOX_KEY or not FOX_URL:
        return 0
    try:
        r = requests.post(
            f"{FOX_URL}/accounts/import/bulk",
            headers=FOX_HEADERS,
            json={"accounts": accounts},
            timeout=60,
        )
        if r.status_code == 200:
            data = r.json()
            added = data.get("added", 0)
            updated = data.get("updated", 0)
            print(f"    🦊 FoxRouters: +{added} new, ~{updated} updated")
            return added + updated
        print(f"    ❌ FoxRouters: {r.status_code} {r.text[:120]}")
        return 0
    except Exception as e:
        print(f"    ❌ FoxRouters: {e}")
        return 0


# ─── Main registration ────────────────────────────────────────

_BASE_DELAY = 8   # seconds between accounts
_MAX_DELAY  = 60  # cap for backoff


def register(email=None, password=None, max_retries=2):
    """
    Register a single Grok AI account.

    Retries up to `max_retries` times on transient failures
    (turnstile, OTP, approve). Email reuse is safe — xAI rejects
    duplicates at the gRPC step, which is treated as permanent.

    Returns dict with tokens on success, None on failure.
    """
    from approve import approve_device

    for attempt in range(1, max_retries + 1):
        t0 = time.time()
        pw = password or DEF_PASS
        if not email:
            email = rand_email()
        if attempt > 1:
            print(f"\n  🔄 retry {attempt}/{max_retries} — {email}")
        else:
            print(f"\n  {email}")

        s = requests.Session()
        s.headers.update({"User-Agent": UA})

        # 1. Turnstile
        ts = solve_turnstile()
        if not ts:
            print("    ❌ turnstile")
            if attempt < max_retries:
                print("    → retrying...")
                time.sleep(3)
                continue
            return None
        print(f"    [1] turnstile ✅")

        # 2. Send OTP
        s.get("https://accounts.x.ai/sign-up", timeout=15)
        _grpc("CreateEmailValidationCode", bytes(_es(1, email)), s)

        # 3. Poll OTP
        otp = poll_otp(email)
        if not otp:
            print("    ❌ no OTP (check D1 config / email routing)")
            if attempt < max_retries:
                print("    → retrying...")
                time.sleep(3)
                continue
            return None
        print(f"    [2] OTP: {otp} ✅")

        # 4. Verify + Register
        _grpc("VerifyEmailValidationCode", bytes(_es(1, email) + _es(2, otp)), s)
        aa = _es(1, ts)
        cur = _es(1, "Test") + _es(2, "User") + _es(3, email) + _es(5, pw) + _ei(6, 1)
        outer = _em(1, cur) + _em(6, aa) + _es(9, otp) + _es(10, str(uuid.uuid4()))
        r = _grpc("CreateUserAndSession", bytes(outer), s)
        if not re.findall(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", r.text):
            # Permanent failure — email exists or turnstile rejected by server
            print("    ❌ register failed (email may exist or turnstile rejected)")
            return None  # Don't retry — gRPC rejection is permanent
        print(f"    [3] registered ✅ ({time.time()-t0:.0f}s)")

        # 5. Device code
        dc = fetch_device_code()
        if not dc:
            print("    ❌ device code failed")
            if attempt < max_retries:
                print("    → retrying...")
                time.sleep(3)
                continue
            return None
        device_code, user_code = dc["device_code"], dc["user_code"]
        print(f"    [4] device: {user_code}")

        # 6. Turnstile for approval
        ts2 = solve_turnstile()
        if not ts2:
            print("    ❌ turnstile (approve)")
            if attempt < max_retries:
                print("    → retrying...")
                time.sleep(3)
                continue
            return None

        # 7. Approve
        print(f"    [5] approving...")
        approved = approve_device(user_code, email, pw, ts2)
        if not approved:
            print(f"    ❌ approve failed")
            if attempt < max_retries:
                print("    → retrying with new email...")
                email = None  # New email for retry
                time.sleep(5)
                continue
            return None

        # 8. Poll token
        access, refresh, id_token = poll_device_token(device_code)
        if not access:
            print(f"    ❌ token poll failed")
            if attempt < max_retries:
                print("    → retrying...")
                time.sleep(3)
                continue
            return None

        elapsed = time.time() - t0
        out = {
            "email": email,
            "password": pw,
            "access_token": access,
            "refresh_token": refresh,
            "id_token": id_token or "",
            "expires_in": 21600,
            "user_id": dec_jwt(access).get("sub", ""),
            "elapsed": round(elapsed, 1),
        }

        # Save token file
        os.makedirs(TOKENS_DIR, exist_ok=True)
        fname = email.split("@")[0]
        with open(os.path.join(TOKENS_DIR, f"{fname}.json"), "w") as f:
            json.dump({
                "access_token": out["access_token"],
                "refresh_token": out["refresh_token"],
                "id_token": out["id_token"],
                "expires_in": out["expires_in"],
                "token_type": "Bearer",
                "scope": SCOPE,
            }, f, indent=2)

        # Append to accounts log
        with open(ACCTS_FILE, "a") as f:
            f.write(json.dumps({
                "email": email,
                "password": pw,
                "user_id": out["user_id"],
                "token_file": f"tokens/{fname}.json",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }) + "\n")

        print(f"    [6] tokens ✅ ({elapsed:.0f}s)")

        # Optional FoxRouters push
        push_to_foxrouter([{
            "email": email,
            "access_token": out["access_token"],
            "refresh_token": out["refresh_token"],
            "id_token": out["id_token"],
            "expires_in": 21600,
        }])

        return out

    return None  # All retries exhausted


# ─── CLI ──────────────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Grok AI Account Registration")
    p.add_argument("-n", "--count", type=int, default=1, help="Number of accounts to register")
    p.add_argument("--email", help="Specific email (otherwise random)")
    p.add_argument("--password", help="Specific password (otherwise config default)")
    args = p.parse_args()

    print(f"🚀 Grok Registration — {args.count} account(s)")
    print(f"📧 Domain: {DOMAIN}")
    print(f"🔑 Password: {'*' * len(DEF_PASS) if DEF_PASS else '(must provide --password)'}")

    ok = []
    consecutive_fails = 0
    for i in range(args.count):
        r = register(
            email=args.email if i == 0 and args.email else None,
            password=args.password,
        )
        if r:
            ok.append(r)
            consecutive_fails = 0
        if i < args.count - 1:
            if r:
                # Success: normal delay
                delay = _BASE_DELAY
            else:
                # Failure: exponential backoff
                consecutive_fails += 1
                delay = min(_BASE_DELAY * (2 ** consecutive_fails), _MAX_DELAY)
                log.info(f"Backoff: {delay}s after {consecutive_fails} consecutive failure(s)")
            time.sleep(delay)

    # Bulk Import JSON output
    bulk = [{
        "email": r["email"],
        "access_token": r["access_token"],
        "refresh_token": r["refresh_token"],
        "id_token": r["id_token"],
        "expires_in": r["expires_in"],
    } for r in ok]

    print(f"\n{'='*60}")
    print(f"  {len(ok)}/{args.count} done")
    print(f"{'='*60}")
    if bulk:
        print(f"\n📋 Bulk Import JSON:\n")
        print(json.dumps(bulk, indent=2))
