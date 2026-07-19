# Grok AI Account Registration

Automated Grok AI (xAI) account registration via **gRPC-Web API** + **CloakBrowser** OAuth device approval.

## How it works

```
┌─────────────────────────────────────────────────────┐
│  1. Solve Cloudflare Turnstile                       │
│     Boterdrop (free) → Capsolver (paid fallback)     │
│                                                      │
│  2. Send OTP via gRPC-Web                            │
│     CreateEmailValidationCode → email arrives         │
│                                                      │
│  3. Poll OTP from Cloudflare D1                      │
│     Catch-all email routing → D1 database query      │
│                                                      │
│  4. Register via gRPC-Web                            │
│     CreateUserAndSession → JWT session               │
│                                                      │
│  5. OAuth Device Code flow                           │
│     Device code → CloakBrowser login → Allow         │
│                                                      │
│  6. Poll token                                       │
│     access_token + refresh_token + id_token          │
└─────────────────────────────────────────────────────┘
```

**Average time:** ~45-65 seconds per account

## Requirements

- **Python 3.10+**
- **Catch-all email domain** with Cloudflare Email Routing → D1 database
- **Turnstile solver** — one of:
  - [Boterdrop Solver](https://github.com/najibyahya/Boterdrop-Solver) (free, requires Camoufox server on port 8005)
  - [Capsolver](https://capsolver.com) (paid, ~$0.003/solve)
- **CloakBrowser** — headless Chromium with anti-detection

## Setup

```bash
# Clone
git clone https://github.com/YOUR_USER/grok-register.git
cd grok-register

# Install
pip install -r requirements.txt

# Configure
cp config.example.json config.json
# Edit config.json with your values (see below)
```

### config.json

| Field | Description |
|-------|-------------|
| `d1.url` | Cloudflare D1 query API URL |
| `d1.token` | Cloudflare API token with D1 scope |
| `capsolver_key` | Capsolver API key (optional, paid fallback) |
| `boterdrop_url` | Boterdrop solver URL (default: `http://127.0.0.1:8005`) |
| `email_domain` | Your catch-all domain (e.g. `yourdomain.com`) |
| `default_password` | Default password for all accounts |
| `foxrouter.url` | FoxRouters API URL (optional, for auto-push) |
| `foxrouter.key` | FoxRouters admin key (optional) |

### Setting up Cloudflare D1 Email Routing

1. Add your domain to Cloudflare
2. Enable **Email Routing** → catch-all → worker
3. Worker stores emails in D1 database
4. Create a D1 database and API token with D1 read scope
5. The OTP polling queries `SELECT subject FROM email WHERE to_email = ?`

The OTP format is `XX-XXX` (e.g. `A9E-WJR`) — extracted from email subject via regex.

### Setting up Boterdrop (Free Turnstile)

```bash
# Run Camoufox-based turnstile solver
# See: https://github.com/najibyahya/Boterdrop-Solver
# Default port: 8005
```

## Usage

### Single account
```bash
python3 register.py
python3 register.py --email custom@yourdomain.com --password 'MyP@ss'
```

### Multiple accounts
```bash
python3 register.py -n 5
```

### Bulk with batch notifications
```bash
python3 bulk.py 100
python3 bulk.py 500 2>&1 | tee run.log
```

### Refresh expired tokens
```bash
python3 refresh_tokens.py              # refresh all tokens/
python3 refresh_tokens.py tokens/alex.json  # single file
```

## Output

### Token files
Each account gets a token file in `tokens/`:
```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "id_token": "eyJ...",
  "expires_in": 21600,
  "token_type": "Bearer",
  "scope": "openid profile email offline_access grok-cli:access api:access conversations:read conversations:write"
}
```

### Accounts log
`accounts.jsonl` — one JSON object per line:
```json
{"email": "alex.nova42@yourdomain.com", "password": "...", "user_id": "...", "token_file": "tokens/alex.nova42.json", "created_at": "2026-07-19T06:00:00+00:00"}
```

### Bulk Import JSON
Both `register.py` and `bulk.py` output a JSON array compatible with **FoxRouters bulk import**:
```json
[
  {
    "email": "alex.nova42@yourdomain.com",
    "access_token": "eyJ...",
    "refresh_token": "eyJ...",
    "id_token": "eyJ...",
    "expires_in": 21600
  }
]
```

## FoxRouters Integration

If `foxrouter.url` and `foxrouter.key` are set in config, accounts are **auto-pushed** to FoxRouters after each successful registration.

```json
{
  "foxrouter": {
    "url": "http://127.0.0.1:20130",
    "key": "your-bootstrap-key"
  }
}
```

## Architecture

| File | Purpose |
|------|---------|
| `register.py` | Core registration logic (gRPC-Web, Turnstile, OTP) |
| `approve.py` | CloakBrowser OAuth device approval |
| `bulk.py` | Batch wrapper with progress notifications |
| `refresh_tokens.py` | Token refresh utility |
| `config.json` | Your configuration (gitignored) |
| `config.example.json` | Template configuration |

## Technical Details

### gRPC-Web Protocol

xAI uses **Connect-ES gRPC-Web** (not REST). Registration calls:

- `POST /auth_mgmt.AuthManagement/CreateEmailValidationCode` — send OTP
- `POST /auth_mgmt.AuthManagement/VerifyEmailValidationCode` — verify OTP
- `POST /auth_mgmt.AuthManagement/CreateUserAndSession` — register + get JWT

Body format: base64-encoded `[0x00][4-byte length][protobuf]`

### Turnstile Interception

CloakBrowser's `page.route()` intercepts the Cloudflare Turnstile script load and replaces `window.turnstile` with a pre-solved token. The callback fires immediately, injecting the token into the form field.

### OAuth Device Code Flow

1. `POST auth.x.ai/oauth2/device/code` → get `user_code` + `device_code`
2. CloakBrowser navigates to device page, logs in, clicks Allow
3. `POST auth.x.ai/oauth2/token` with `grant_type=urn:ietf:params:oauth:grant-type:device_code` → get tokens

## Rate Limits

- **xAI device codes:** ~3-5 requests before `slow_down` (HTTP 429). Built-in retry with backoff.
- **Turnstile:** Each token is single-use. Fresh solve needed per registration + per approval.
- **D1 OTP:** ~5s polling interval. No known rate limit.
- **Recommended:** 8-second delay between accounts in bulk mode.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `❌ turnstile` | Check Boterdrop is running (`curl http://localhost:8005/health`) or set `capsolver_key` |
| `❌ no OTP` | Verify D1 config, catch-all routing enabled, email domain in config |
| `❌ register failed` | Email may already exist, or Turnstile token was rejected |
| `❌ approve failed` | CloakBrowser crashed — try with `--no-sandbox`, check memory |
| `❌ token timeout` | Approval took too long — xAI rate limited device code |
| `HTTP 429 slow_down` | Back off: increase delay between accounts |

## License

MIT

## ⚠️ Security Notes

- **`config.json` contains your API tokens** — never commit it. The `.gitignore` blocks it.
- **`accounts.jsonl` stores passwords in plaintext** — this is intentional (you need them to log in), but treat the file as a secret. `chmod 600 accounts.jsonl`.
- **`tokens/` directory** contains OAuth tokens — also gitignored. Treat as credentials.
- Run `chmod 600 config.json` after creating it.
