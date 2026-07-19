#!/usr/bin/env python3
"""
CloakBrowser OAuth Device Approval
===================================
Launches headless browser to approve OAuth device codes on accounts.x.ai.
Uses Turnstile intercept to inject pre-solved tokens.

Requires: pip install cloakbrowser
"""

import time, re, json, logging
import cloakbrowser

log = logging.getLogger("grok-register")

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
]


def setup_turnstile_intercept(page, ts_token):
    """Intercept Cloudflare Turnstile script and inject pre-solved token."""
    fake = f"""(function(){{
        var T={json.dumps(ts_token)};
        window.turnstile={{
            _t:T, render:function(c,p){{
                var id='w'+Math.random();
                setTimeout(function(){{
                    if(p&&p.callback)p.callback(T);
                    var i=document.querySelector('input[name=cf-turnstile-response]');
                    if(i){{var s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
                    s.call(i,T);i.dispatchEvent(new Event('input',{{bubbles:true}}));
                    i.dispatchEvent(new Event('change',{{bubbles:true}}));}}
                }},30);
                return id;
            }},
            getResponse:function(){{return T}},
            reset:function(){{}},remove:function(){{}},execute:function(){{}},
            isExpired:function(){{return false}}
        }};
    }})();"""
    ts_re = re.compile(r"challenges\.cloudflare\.com/turnstile|turnstile/v0/api\.js")

    def on_route(route):
        if ts_re.search(route.request.url):
            m = re.search(r"onload=([a-zA-Z0-9_]+)", route.request.url)
            body = fake
            if m:
                body += f"\nif(typeof window['{m.group(1)}']==='function')window['{m.group(1)}']();\n"
            route.fulfill(status=200, content_type="application/javascript", body=body)
        else:
            route.continue_()

    page.route("**/*", on_route)


def click_allow(page, max_tries=3):
    """Click the Allow button on consent page."""
    for i in range(max_tries):
        try:
            btn = page.locator("button:has-text('Allow')")
            if btn.count() > 0:
                btn.first.click(timeout=3000)
                time.sleep(1)
                return True
        except Exception:
            pass
        try:
            btn = page.locator("button:has-text('Izinkan')")
            if btn.count() > 0:
                btn.first.click(timeout=3000)
                time.sleep(1)
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def approve_device(user_code, email, password, ts_token, proxy=None):
    """
    Approve OAuth device code via headless CloakBrowser.

    Args:
        user_code: OAuth device user code (e.g. "ABCD-EFGH")
        email: Account email
        password: Account password
        ts_token: Pre-solved Turnstile token
        proxy: Optional proxy dict {"server": "socks5://...", "username": "...", "password": "..."}

    Returns:
        True if the browser reached /device/done or /account after login+allow.
        NOTE: This indicates the UI flow completed, NOT that the token is ready.
        The caller must still call poll_device_token() to exchange the device_code.
    """
    browser = None
    try:
        launch_kwargs = dict(
            headless=True,
            args=CHROMIUM_ARGS,
            humanize=False,
        )
        if proxy:
            launch_kwargs["proxy"] = proxy

        browser = cloakbrowser.launch(**launch_kwargs)
        page = browser.new_page()
        setup_turnstile_intercept(page, ts_token)

        # Navigate to device authorization page
        page.goto(
            f"https://accounts.x.ai/oauth2/device?user_code={user_code}",
            wait_until="domcontentloaded",
            timeout=45000,
        )
        time.sleep(1.5)

        # Vietnamese button variant
        try:
            page.click("button:has-text('Chấp nhận')", timeout=1500)
            time.sleep(0.5)
        except Exception:
            pass

        # Click Continue
        try:
            with page.expect_navigation(timeout=10000, wait_until="domcontentloaded"):
                page.click("button:has-text('Continue')", timeout=4000)
        except Exception:
            try:
                page.click("button:has-text('Continue')", timeout=3000)
                time.sleep(2)
            except Exception:
                pass

        # Already on consent page?
        url = page.url
        if "consent" in url or "device" in url:
            try:
                if page.locator("button:has-text('Allow')").count() > 0:
                    click_allow(page)
            except Exception:
                pass

        # Login with email
        try:
            page.click("button:has-text('Login with email')", timeout=6000)
            time.sleep(1.2)
        except Exception:
            pass

        # Fill email
        try:
            page.fill("input[type=email]", email, timeout=4000)
        except Exception:
            try:
                page.locator("input:not([type=hidden]):not([type=password])").first.fill(email)
            except Exception:
                pass
        time.sleep(0.2)

        # Click Next
        try:
            page.click("button:has-text('Next')", timeout=4000)
            time.sleep(3)
        except Exception:
            pass

        # Fill password
        try:
            page.fill("input[type=password]", password, timeout=5000)
        except Exception:
            pass

        # Inject turnstile response into hidden field
        try:
            page.evaluate("""(token) => {
                var i = document.querySelector('input[name=cf-turnstile-response]');
                if(i) {
                    var s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    s.call(i, token);
                    i.dispatchEvent(new Event('input', {bubbles: true}));
                    i.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""", ts_token)
        except Exception:
            pass

        # Click Login
        try:
            page.click("button:has-text('Login')", timeout=4000)
        except Exception:
            try:
                page.click("button[type=submit]", timeout=3000)
            except Exception:
                pass

        # Wait for post-login redirects
        try:
            page.wait_for_url("**/account**", timeout=30000)
        except Exception:
            time.sleep(8)

        # Allow on consent page
        click_allow(page)

        # Wait for device done
        try:
            page.wait_for_url("**/device/done**", timeout=20000)
            return True
        except Exception:
            time.sleep(3)
            # Verify we actually landed on a success page, not still on consent
            url = page.url
            if "device/done" in url:
                return True
            if "account" in url and "error" not in url and "consent" not in url:
                return True
            log.warning(f"approve: unexpected final URL: {url}")
            return False

    except Exception as e:
        print(f"    ❌ approve error: {e}")
        return False
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
