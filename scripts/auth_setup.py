#!/usr/bin/env python3
"""Institutional Library Authentication & Session Manager using Playwright.

Dedicated to handling one-time institutional SSO authentication, cookie capture,
session persistence, dynamic EZproxy URL rewriting, and resilient 2FA/SSO fallback.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import select
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_CONFIG_FILE = ROOT / "config" / "active_institution.json"
UNIVERSITIES_FILE = ROOT / "config" / "universities.json"
ENV_FILE = ROOT / ".env"
DEFAULT_STATE_PATH = ROOT / "state" / "storage_state.json"
COMPAT_STATE_PATH = ROOT / "institutional_auth.json"
DEFAULT_PROFILE_DIR = ROOT / "state" / "chromium_profile"

DEFAULT_DESKTOP_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
STEALTH_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-infobars",
    "--disable-dev-shm-usage"
]

AUTH_CHALLENGE_PROMPT = "Institutional session expired or 2FA required. Please complete authentication in the browser window..."

SSO_HOST_PATTERNS = [
    "duo.",
    "duosecurity.com",
    "okta.com",
    "onevu.",
    "shibboleth",
    "/saml",
    "/sso",
    "/cas/",
    "/idp/",
    "login.microsoftonline.com",
    "pingidentity",
    "openathens.net",
    "auth.",
]

CHALLENGE_TEXT_PATTERNS = [
    "duo security",
    "duo push",
    "check for a duo push",
    "duo-universal-prompt",
    "institutional session expired",
    "session has expired",
    "session expired",
    "session timed out",
    "two-factor authentication",
    "multi-factor authentication",
    "2-step verification",
    "verify your identity",
    "enter your user name and password",
    "enter your username and password",
    "sign in with your organizational account",
    "log in to vanderbilt university",
    "single sign-on",
]


class InstitutionalAuthChallenge(Exception):
    """Raised when an institutional login wall, SSO redirect, or 2FA challenge is encountered."""

    def __init__(self, message="Institutional session expired or 2FA required.", target_url=None):
        super().__init__(message)
        self.message = message
        self.target_url = target_url


def is_login_or_challenge(url=None, html=None, page=None, institution=None):
    """Detect if the given URL, page content, or Playwright page represents an institutional login wall or 2FA/SSO challenge."""
    inst = institution or get_active_institution()
    login_portal = (inst.get("login_portal") or "").lower()

    current_url = ""
    if page:
        try:
            current_url = (page.url or "").lower()
        except Exception:
            pass
    elif url:
        current_url = url.lower()

    if current_url:
        if any(pat in current_url for pat in SSO_HOST_PATTERNS):
            return True
        if login_portal and login_portal in current_url:
            return True
        if "login?url=" in current_url or ("/login" in current_url and ("proxy" in current_url or "library" in current_url or "ezproxy" in current_url)):
            return True

    if page:
        try:
            # Check for Duo iframe or widget
            duo_selectors = [
                "iframe#duo_iframe",
                "iframe[src*='duosecurity']",
                "iframe[src*='duo.com']",
                "duo-universal-prompt",
                "#duo-parent",
                "[data-duo-host]",
            ]
            for sel in duo_selectors:
                if page.locator(sel).count() > 0:
                    return True

            has_password = page.locator("input[type='password']").count() > 0
            if has_password:
                title = (page.title() or "").lower()
                if any(k in current_url or k in title for k in ["login", "signin", "sign-in", "auth", "sso", "saml", "idp", "proxy", "vanderbilt", "okta", "duo"]):
                    return True

            body_text = (page.inner_text("body") or "").lower()
            if any(term in body_text for term in CHALLENGE_TEXT_PATTERNS):
                if has_password or any(pat in current_url for pat in SSO_HOST_PATTERNS) or "login" in current_url or "proxy" in current_url or len(body_text) < 5000:
                    return True
        except Exception:
            pass

    if html:
        lower_html = html.lower()
        if any(term in lower_html for term in CHALLENGE_TEXT_PATTERNS):
            if "type=\"password\"" in lower_html or "type='password'" in lower_html or any(pat in current_url for pat in SSO_HOST_PATTERNS) or "login" in current_url:
                return True

    return False


def load_env_vars():
    """Load variables from .env file into os.environ if present."""
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("\"'")
                if k and k not in os.environ:
                    os.environ[k] = v


def get_active_institution():
    """Dynamically resolve the active institution configuration.

    Resolution order:
    1. config/active_institution.json
    2. Environment variables (.env or os.environ)
    3. Default to Vanderbilt University in config/universities.json
    """
    load_env_vars()

    # 1. Check config/active_institution.json
    if ACTIVE_CONFIG_FILE.is_file():
        try:
            with open(ACTIVE_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("ezproxy_prefix"):
                    return data
        except Exception:
            pass

    # 2. Check environment variables
    env_id = os.environ.get("ACTIVE_INSTITUTION_ID")
    env_proxy = os.environ.get("ACTIVE_INSTITUTION_PROXY_PREFIX")
    if env_proxy:
        return {
            "id": env_id or "custom",
            "name": os.environ.get("ACTIVE_INSTITUTION_NAME", "Configured Institution"),
            "country": os.environ.get("ACTIVE_INSTITUTION_COUNTRY", "United States"),
            "ezproxy_prefix": env_proxy,
            "library_portal": os.environ.get("ACTIVE_INSTITUTION_LIBRARY_PORTAL", ""),
            "login_portal": os.environ.get("ACTIVE_INSTITUTION_LOGIN_PORTAL", env_proxy.split("?")[0]),
        }

    # 3. Fallback to Vanderbilt University in config/universities.json
    if UNIVERSITIES_FILE.is_file():
        try:
            with open(UNIVERSITIES_FILE, "r", encoding="utf-8") as f:
                universities = json.load(f)
                vanderbilt = next((u for u in universities if u.get("id") == "vanderbilt"), None)
                if vanderbilt:
                    return vanderbilt
                if universities:
                    return universities[0]
        except Exception:
            pass

    # Hardcoded default fallback
    return {
        "id": "vanderbilt",
        "name": "Vanderbilt University",
        "country": "United States",
        "ezproxy_prefix": "http://proxy.library.vanderbilt.edu/login?url=",
        "library_portal": "https://www.library.vanderbilt.edu",
        "login_portal": "http://proxy.library.vanderbilt.edu/login",
    }


def get_proxied_url(target_url, institution=None):
    """Rewrite a target article URL or DOI through the institution's EZproxy gateway."""
    inst = institution or get_active_institution()
    proxy_prefix = inst.get("ezproxy_prefix", "").strip()

    if not proxy_prefix:
        return target_url

    clean_url = target_url.strip()

    # If already proxied, return as-is
    if clean_url.startswith(proxy_prefix):
        return clean_url

    # Normalize DOI syntax
    doi_match = re.match(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", clean_url, re.I)
    if doi_match:
        doi = doi_match.group(1)
        clean_url = f"https://doi.org/{doi}"

    return f"{proxy_prefix}{clean_url}"


def get_existing_state_path(custom_path=None):
    """Return the path to an existing storage state file, checking default locations."""
    if custom_path:
        p = Path(custom_path)
        if p.is_file() and p.stat().st_size > 0:
            return p
    if DEFAULT_STATE_PATH.is_file() and DEFAULT_STATE_PATH.stat().st_size > 0:
        return DEFAULT_STATE_PATH
    if COMPAT_STATE_PATH.is_file() and COMPAT_STATE_PATH.stat().st_size > 0:
        return COMPAT_STATE_PATH
    return None


def authenticate_interactive(institution=None, target_url=None, state_path=None, headless=False, timeout_sec=300, profile_dir=None):
    """Launch Playwright browser for user authentication and save storage state.

    Uses Playwright's persistent context pointing to state/chromium_profile/ to preserve
    session cookies and Duo authentication states across runs.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: Playwright is not installed. Run: pip install playwright && playwright install chromium", file=sys.stderr)
        return False

    inst = institution or get_active_institution()
    save_path = Path(state_path or DEFAULT_STATE_PATH).resolve()
    save_path.parent.mkdir(parents=True, exist_ok=True)
    p_dir = Path(profile_dir or os.environ.get("CHROMIUM_PROFILE_DIR") or DEFAULT_PROFILE_DIR).resolve()
    p_dir.mkdir(parents=True, exist_ok=True)

    destination_url = target_url or inst.get("login_portal") or inst.get("library_portal")
    if target_url and not target_url.startswith(inst.get("ezproxy_prefix", "")):
        destination_url = get_proxied_url(target_url, inst)

    print("=" * 70)
    print(" 🔑 Institutional Library Authentication (Playwright)")
    print("=" * 70)
    print(f"Active Institution: {inst.get('name')} [{inst.get('id')}]")
    print(f"EZproxy Prefix:     {inst.get('ezproxy_prefix')}")
    print(f"Library Portal:     {inst.get('library_portal')}")
    print(f"Login Destination:  {destination_url}")
    print(f"Session Save Path:  {save_path}")
    print(f"Profile Directory:  {p_dir}")
    print("-" * 70)

    if not headless:
        print(f"\n{AUTH_CHALLENGE_PROMPT}")
        print("   (The session will save automatically once login completes, or you can press [ENTER] when done)\n")

    with sync_playwright() as p:
        context = None
        browser = None
        existing_state = get_existing_state_path(state_path)

        if hasattr(p.chromium, "launch_persistent_context"):
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(p_dir),
                    headless=headless,
                    args=list(STEALTH_BROWSER_ARGS),
                    user_agent=DEFAULT_DESKTOP_USER_AGENT,
                    accept_downloads=True,
                )
            except Exception as e:
                print(f"  Note launching persistent context: {e}")

        if context is None:
            browser = p.chromium.launch(
                headless=headless,
                args=list(STEALTH_BROWSER_ARGS)
            )
            context_kwargs = {}
            if existing_state:
                try:
                    context_kwargs["storage_state"] = str(existing_state)
                    print(f"Loaded existing session state from {existing_state.name}.")
                except Exception:
                    pass

            context = browser.new_context(
                **context_kwargs,
                user_agent=DEFAULT_DESKTOP_USER_AGENT
            )

        # Inject existing state cookies explicitly into context cookie store
        if existing_state and hasattr(context, "add_cookies"):
            try:
                with open(existing_state, "r", encoding="utf-8") as f:
                    s_data = json.load(f)
                    c_list = s_data.get("cookies", [])
                    if c_list:
                        cleaned = []
                        for c in c_list:
                            c_copy = dict(c)
                            if c_copy.get("sameSite") not in ("Strict", "Lax", "None"):
                                c_copy.pop("sameSite", None)
                            cleaned.append(c_copy)
                        context.add_cookies(cleaned)
            except Exception:
                pass

        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.pages[0] if (hasattr(context, "pages") and context.pages) else context.new_page()

        print(f"Opening browser to: {destination_url}")
        try:
            page.goto(destination_url, timeout=45000)
        except Exception as e:
            print(f"Navigation note: {e}")

        if not headless:
            start_time = time.time()
            initial_url = page.url
            while time.time() - start_time < timeout_sec:
                try:
                    if page.is_closed() or (browser and not browser.is_connected()):
                        print("Browser window closed.")
                        break

                    current_url = page.url
                    cookies = context.cookies()

                    # Detect if user has successfully navigated through SSO/SAML/Duo
                    challenge_active = is_login_or_challenge(page=page, institution=inst)
                    has_auth_cookies = any("ezp" in c["name"].lower() or "proxy" in c["name"].lower() or "session" in c["name"].lower() for c in cookies)

                    if not challenge_active and current_url != initial_url and (has_auth_cookies or "proxy" in current_url or "vanderbilt" in current_url):
                        print(f"\n✓ Detected authenticated destination: {current_url}")
                        page.wait_for_timeout(2000)
                        break

                    # Check for terminal ENTER key without blocking
                    try:
                        if sys.stdin and not sys.stdin.closed:
                            if select.select([sys.stdin], [], [], 0)[0]:
                                sys.stdin.readline()
                                print("\nConfirmation received from terminal.")
                                break
                    except Exception:
                        pass

                    page.wait_for_timeout(1500)
                except Exception:
                    break
        else:
            print("Headless mode: Waiting for page to settle...")
            page.wait_for_timeout(5000)

        # Save session cookies and storage state
        context.storage_state(path=str(save_path))
        try:
            context.storage_state(path=str(COMPAT_STATE_PATH))
        except Exception:
            pass

        if browser:
            browser.close()
        elif context:
            context.close()

    print(f"\n✓ Institutional authentication state successfully saved to:")
    print(f"  • {save_path}")
    print(f"  • {COMPAT_STATE_PATH}")
    print("Downstream tools and paper retrieval pipelines can now reuse this session for subscription recovery.")
    return True


# Backward-compatible convenience alias
login = authenticate_interactive


def validate_session(state_path=None, institution=None, test_url=None, interactive_fallback=True, timeout_sec=20):
    """Validate saved institutional session state.

    If session state is missing, or if loading an institutional resource
    hits a login wall or 2FA prompt and fails headless cookies, catch the state,
    reopen Playwright in headed mode (headless=False), notify user in the terminal,
    pause until authentication completes, and save the fresh session state.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: Playwright is not installed.", file=sys.stderr)
        return False

    inst = institution or get_active_institution()
    session_file = get_existing_state_path(state_path)
    save_path = Path(state_path or DEFAULT_STATE_PATH).resolve()

    if not session_file:
        print(f"No saved session found at {save_path}.")
        if interactive_fallback:
            print(f"\n{AUTH_CHALLENGE_PROMPT}\n")
            return authenticate_interactive(
                institution=inst,
                target_url=test_url,
                state_path=save_path,
                headless=False,
            )
        return False

    check_url = test_url or inst.get("login_portal") or inst.get("library_portal")
    print(f"Validating institutional session against {check_url} in headless mode...")

    hit_challenge = False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=list(STEALTH_BROWSER_ARGS)
            )
            context = browser.new_context(
                storage_state=str(session_file),
                user_agent=DEFAULT_DESKTOP_USER_AGENT
            )
            # Inject session cookies explicitly into context cookie store
            if hasattr(context, "add_cookies"):
                try:
                    with open(session_file, "r", encoding="utf-8") as f:
                        s_data = json.load(f)
                        c_list = s_data.get("cookies", [])
                        if c_list:
                            cleaned = []
                            for c in c_list:
                                c_copy = dict(c)
                                if c_copy.get("sameSite") not in ("Strict", "Lax", "None"):
                                    c_copy.pop("sameSite", None)
                                cleaned.append(c_copy)
                            context.add_cookies(cleaned)
                except Exception:
                    pass
            page = context.new_page()
            try:
                page.goto(check_url, timeout=timeout_sec * 1000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                if is_login_or_challenge(page=page, institution=inst):
                    hit_challenge = True
            except Exception as e:
                print(f"Validation navigation note: {e}")
            finally:
                browser.close()
    except Exception as e:
        print(f"Validation browser note: {e}")
        hit_challenge = True

    if hit_challenge:
        print("\nSession validation detected institutional login wall / 2FA challenge.")
        if interactive_fallback:
            print(f"\n{AUTH_CHALLENGE_PROMPT}\n")
            return authenticate_interactive(
                institution=inst,
                target_url=test_url or check_url,
                state_path=save_path,
                headless=False,
            )
        return False

    print("✓ Saved institutional session is valid.")
    return True


def check_session(state_path=None, validate=False, interactive_fallback=True, institution=None):
    """Check if saved session state exists and report cookie count. Optionally validates live."""
    if validate:
        return validate_session(state_path=state_path, institution=institution, interactive_fallback=interactive_fallback)

    existing_state = get_existing_state_path(state_path)
    if not existing_state:
        print(f"No saved session found at {state_path or DEFAULT_STATE_PATH}.")
        return False

    try:
        with open(existing_state, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies", [])
        origins = data.get("origins", [])
        print(f"✓ Saved session state found at {existing_state}:")
        print(f"  • Cookies count: {len(cookies)}")
        print(f"  • Origins count: {len(origins)}")
        domains = sorted({c.get("domain") for c in cookies if c.get("domain")})
        if domains:
            print(f"  • Authenticated domains: {', '.join(domains[:5])}" + ("..." if len(domains) > 5 else ""))
        return True
    except Exception as e:
        print(f"Error reading session state: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Target paper URL or login destination")
    parser.add_argument("--check", action="store_true", help="Check if existing authenticated session state exists")
    parser.add_argument("--validate", action="store_true", help="Perform live session validation and trigger 2FA/SSO fallback if expired")
    parser.add_argument("--proxy-url", help="Rewrite and print the proxied URL for a given DOI or article link")
    parser.add_argument("--state-path", help=f"Custom path for session state JSON (default: {DEFAULT_STATE_PATH})")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--info", action="store_true", help="Display active institution library & proxy information")
    args = parser.parse_args()

    inst = get_active_institution()

    if args.info:
        print(f"Active Institution: {inst.get('name')} [{inst.get('id')}]")
        print(f"EZproxy Prefix:     {inst.get('ezproxy_prefix')}")
        print(f"Library Portal:     {inst.get('library_portal')}")
        print(f"Login Portal:       {inst.get('login_portal')}")
        return 0

    if args.proxy_url:
        proxied = get_proxied_url(args.proxy_url, inst)
        print(proxied)
        return 0

    if args.validate:
        valid = validate_session(state_path=args.state_path, institution=inst, test_url=args.url, interactive_fallback=not args.headless)
        return 0 if valid else 1

    if args.check:
        valid = check_session(args.state_path)
        return 0 if valid else 1

    success = authenticate_interactive(
        institution=inst,
        target_url=args.url,
        state_path=args.state_path,
        headless=args.headless
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
