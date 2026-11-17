#!/usr/bin/env python3
"""Institutional Library Authentication & Proxy Routing using Playwright.

Dynamically loads the active institution configured via scripts/select_university.py
(or config/active_institution.json / .env) and manages authenticated browser sessions,
EZproxy URL rewriting, and automated subscription paper retrieval.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_CONFIG_FILE = ROOT / "config" / "active_institution.json"
UNIVERSITIES_FILE = ROOT / "config" / "universities.json"
ENV_FILE = ROOT / ".env"
DEFAULT_STATE_PATH = ROOT / "state" / "storage_state.json"
COMPAT_STATE_PATH = ROOT / "institutional_auth.json"
DOWNLOADS_DIR = ROOT / "downloads"


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
    3. First available entry in config/universities.json
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

    # 3. Fallback to the first university listed in config/universities.json
    if UNIVERSITIES_FILE.is_file():
        try:
            with open(UNIVERSITIES_FILE, "r", encoding="utf-8") as f:
                universities = json.load(f)
                if universities:
                    return universities[0]
        except Exception:
            pass

    # Generic fallback if no config files exist at all
    return {
        "id": "generic",
        "name": "Configured Institution",
        "country": "United States",
        "ezproxy_prefix": "https://ezproxy.institution.edu/login?url=",
        "library_portal": "https://library.institution.edu",
        "login_portal": "https://ezproxy.institution.edu/login",
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


def authenticate_interactive(institution=None, target_url=None, state_path=None, headless=False, timeout_sec=180):
    """Launch Playwright browser for user authentication and save storage state."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: Playwright is not installed. Run: pip install playwright && playwright install chromium", file=sys.stderr)
        return False

    inst = institution or get_active_institution()
    save_path = Path(state_path or DEFAULT_STATE_PATH).resolve()
    save_path.parent.mkdir(parents=True, exist_ok=True)

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
    print("-" * 70)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context_kwargs = {}
        existing_state = get_existing_state_path(state_path)
        if existing_state:
            try:
                context_kwargs["storage_state"] = str(existing_state)
                print(f"Loaded existing session state from {existing_state.name}.")
            except Exception:
                pass

        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        print(f"Opening browser to: {destination_url}")
        try:
            page.goto(destination_url, timeout=30000)
        except Exception as e:
            print(f"Navigation note: {e}")

        if not headless:
            print("\n👉 Please complete your institutional login and MFA in the browser window.")
            print("   (The session will save automatically once login completes, or you can press [ENTER] when done)")

            start_time = time.time()
            initial_url = page.url
            
            # Extract domain keywords dynamically from the institution config to detect return to portal
            portal_domain = ""
            lib_portal = inst.get("library_portal", "")
            if "://" in lib_portal:
                portal_domain = lib_portal.split("://")[1].split("/")[0].lower()

            while time.time() - start_time < timeout_sec:
                try:
                    if page.is_closed() or not browser.is_connected():
                        print("Browser window closed.")
                        break

                    current_url = page.url
                    cookies = context.cookies()
                    
                    # Detect if user has navigated past login/SSO back to library domain or proxy session
                    is_sso = any(auth_host in current_url.lower() for auth_host in ["okta.com", "duo.", "shibboleth", "/saml", "/sso", "login.microsoftonline.com"])
                    has_auth_cookies = any("ezp" in c["name"].lower() or "proxy" in c["name"].lower() or "session" in c["name"].lower() for c in cookies)
                    
                    at_home_domain = portal_domain and portal_domain in current_url.lower()

                    if not is_sso and current_url != initial_url and (has_auth_cookies or "proxy" in current_url or at_home_domain):
                        print(f"\n✓ Detected authenticated destination: {current_url}")
                        page.wait_for_timeout(2000)
                        break

                    # Check for terminal ENTER key without blocking
                    try:
                        import select
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

        browser.close()

    print(f"\n✓ Institutional authentication state successfully saved to:")
    print(f"  • {save_path}")
    print(f"  • {COMPAT_STATE_PATH}")
    print("Downstream tools and paper-fulltext-access can now reuse this session for subscription recovery.")
    return True


def download_paper(identifier, output_path=None, state_path=None, headless=True, institution=None):
    """Retrieve and download a full-text PDF using the authenticated session."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: Playwright is not installed.", file=sys.stderr)
        return False

    inst = institution or get_active_institution()
    existing_state = get_existing_state_path(state_path)
    if not existing_state:
        print("Error: No saved authentication session found. Run 'python3 auth_setup.py' first.", file=sys.stderr)
        return False

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    target_doi = identifier
    doi_match = re.search(r"10\.\d{4,9}/\S+", identifier)
    if doi_match:
        target_doi = doi_match.group(0).rstrip(".,;")

    # Prefer LibKey direct resolver, falling back to proxied DOI
    libkey_url = f"https://libkey.io/{target_doi}"
    proxied_url = get_proxied_url(f"https://doi.org/{target_doi}", inst)

    out_file = Path(output_path) if output_path else (DOWNLOADS_DIR / f"{target_doi.replace('/', '_')}.pdf")
    out_file.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print(" 📥 Downloading Paper via Institutional Session")
    print("=" * 70)
    print(f"Target DOI:        {target_doi}")
    print(f"Institution:       {inst.get('name')}")
    print(f"Session State:     {existing_state}")
    print(f"Output Destination:{out_file}")
    print("-" * 70)

    download_success = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            storage_state=str(existing_state),
            accept_downloads=True,
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            # 1. First attempt: Direct publisher binary endpoints (e.g. Wiley pdfdirect)
            direct_endpoints = [
                get_proxied_url(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{target_doi}", inst),
                get_proxied_url(f"https://onlinelibrary.wiley.com/doi/pdf/{target_doi}", inst)
            ]
            for ep in direct_endpoints:
                try:
                    res = context.request.get(ep, timeout=20000)
                    body = res.body()
                    if body.startswith(b"%PDF-"):
                        out_file.write_bytes(body)
                        download_success = True
                        print(f"✓ Downloaded PDF directly via binary endpoint: {ep}")
                        break
                except Exception:
                    pass

            # 2. Second attempt: Navigate to proxied publisher page and detect PDF iframe / links
            if not download_success:
                print(f"Navigating to proxied article page: {proxied_url}")
                page.goto(proxied_url, timeout=35000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                # Check iframe / embed src
                pdf_frame = page.locator("iframe#pdf-iframe, iframe[src*='pdf'], embed[src*='pdf']")
                if pdf_frame.count() > 0:
                    src = pdf_frame.first.get_attribute("src")
                    if src:
                        from urllib.parse import urljoin
                        full_pdf_url = urljoin(page.url, src)
                        print(f"Fetching embedded PDF frame: {full_pdf_url}")
                        res = context.request.get(full_pdf_url, timeout=25000)
                        body = res.body()
                        if body.startswith(b"%PDF-"):
                            out_file.write_bytes(body)
                            download_success = True
                            print("✓ Downloaded PDF from embedded frame stream.")

                # Check on-page links matching pdfdirect or pdf
                if not download_success:
                    links = page.eval_on_selector_all("a[href*='pdf']", "elements => elements.map(e => e.href)")
                    for href in links:
                        if "/pdfdirect/" in href or "/pdf/" in href or href.endswith(".pdf"):
                            print(f"Testing PDF link: {href}")
                            try:
                                res = context.request.get(href, timeout=20000)
                                body = res.body()
                                if body.startswith(b"%PDF-"):
                                    out_file.write_bytes(body)
                                    download_success = True
                                    print(f"✓ Downloaded PDF from page link: {href}")
                                    break
                            except Exception:
                                pass

            # 3. Third attempt: LibKey fallback
            if not download_success:
                print(f"Trying LibKey resolver: {libkey_url}")
                page.goto(libkey_url, timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                selectors = [
                    ".pdf-download-link",
                    "a.article-pdfLink",
                    "button:has-text('Download PDF')",
                    "a:has-text('Download PDF')",
                    "a[href*='pdf']"
                ]
                for sel in selectors:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        try:
                            with page.expect_download(timeout=15000) as download_info:
                                loc.first.click()
                            download = download_info.value
                            download.save_as(str(out_file))
                            download_success = True
                            print("✓ Downloaded via LibKey download event.")
                            break
                        except Exception:
                            pass
        except Exception as exc:
            print(f"Download navigation note: {exc}")
        finally:
            browser.close()

    if download_success and out_file.is_file() and out_file.stat().st_size > 0:
        print(f"\n✓ Successfully downloaded PDF ({out_file.stat().st_size:,} bytes):")
        print(f"  {out_file}")
        return True
    else:
        print(f"\nCould not automatically trigger file download via headless browser.")
        print(f"Please open the proxied link or LibKey in your browser:")
        print(f"  • LibKey:  {libkey_url}")
        print(f"  • Proxied: {proxied_url}")
        return False


def check_session(state_path=None):
    """Check if saved session state exists and report cookie count."""
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
    parser.add_argument("--url", help="Target paper URL or DOI to access through the institutional proxy")
    parser.add_argument("--download", "-d", help="Download PDF for DOI or URL using the authenticated session")
    parser.add_argument("--output", "-o", help="Output path for downloaded PDF")
    parser.add_argument("--check", action="store_true", help="Check if existing authenticated session state exists")
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

    if args.check:
        valid = check_session(args.state_path)
        return 0 if valid else 1

    if args.download:
        success = download_paper(
            identifier=args.download,
            output_path=args.output,
            state_path=args.state_path,
            headless=args.headless,
            institution=inst
        )
        return 0 if success else 1

    success = authenticate_interactive(
        institution=inst,
        target_url=args.url,
        state_path=args.state_path,
        headless=args.headless
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
