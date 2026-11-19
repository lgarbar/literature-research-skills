#!/usr/bin/env python3
"""End-to-End Literature Pipeline: Download -> Structured Markdown -> Zotero Sync/Intake.

Orchestrates paper retrieval via institutional SSO / EZproxy session, converts
retrieved PDFs into clean structured Markdown (via PyMuPDF4LLM), and either
programmatically syncs the item to Zotero (via PyZotero) or exports a verified
Zotero intake package.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import unquote, urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skills" / "literature-search-review" / "scripts"))

import auth_setup
import source_files
try:
    import catalogue
except ImportError:
    catalogue = None

try:
    import pymupdf4llm
except ImportError:
    pymupdf4llm = None

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    from pyzotero import zotero
except ImportError:
    zotero = None


MIN_PDF_SIZE_BYTES = 100 * 1024  # 100 KB threshold (102,400 bytes)
PRINT_CAPTURE_TAG = "[Print Capture - Not Publisher Formatted PDF]"
MANUAL_PDF_URLS_JSON = "manual_pdf_urls.json"
MANUAL_PDF_URLS_TXT = "manual_pdf_urls.txt"


class InvalidPDFError(RuntimeError):
    """Raised when a retrieved PDF stream or file is invalid, truncated, or an HTML stub."""
    pass


def validate_pdf_stream(content_or_path, min_size=MIN_PDF_SIZE_BYTES, allow_print_fallback=False):
    """Verify that content or file is a valid binary PDF stream.

    Validates:
    - Magic bytes start with b"%PDF-"
    - File size is strictly greater than min_size (100 KB) for official publisher PDFs,
      or size > 500 bytes when allow_print_fallback=True.
    - Rejects raw HTML challenge/error pages and invalid stubs.

    Raises:
        InvalidPDFError: If validation fails.
    """
    if isinstance(content_or_path, (str, Path)):
        p = Path(content_or_path)
        if not p.is_file():
            raise InvalidPDFError(f"Target PDF file does not exist: {p}")
        blob = p.read_bytes()
        label = p.name
    elif isinstance(content_or_path, (bytes, bytearray)):
        blob = bytes(content_or_path)
        label = "PDF stream"
    else:
        raise InvalidPDFError(f"Expected file path or bytes, got {type(content_or_path).__name__}")

    size = len(blob)
    if not blob.startswith(b"%PDF-"):
        # Check if response is an HTML page or error screen
        prefix = blob[:500].decode("utf-8", errors="replace").strip().lower()
        if "<html" in prefix or "<!doctype html" in prefix:
            raise InvalidPDFError(
                f"Retrieved {label} is an HTML document ({size:,} bytes) rather than a binary PDF stream. "
                "Publisher page returned rendered HTML fallback or challenge screen."
            )
        raise InvalidPDFError(
            f"Retrieved {label} missing %PDF- magic bytes header. Starting bytes: {blob[:20]!r}"
        )

    effective_min_size = 500 if allow_print_fallback else min_size
    if size <= effective_min_size:
        threshold_desc = "threshold" if allow_print_fallback else "100 KB minimum threshold"
        raise InvalidPDFError(
            f"Retrieved {label} size ({size:,} bytes) is not greater than the required {threshold_desc} "
            f"({effective_min_size:,} bytes). Rejected possible print fallback, thumbnail, or invalid stub."
        )

    return True


def is_valid_pdf_stream(content_or_path, min_size=MIN_PDF_SIZE_BYTES, allow_print_fallback=False):
    """Convenience helper returning True if content/file is a valid binary PDF stream."""
    try:
        validate_pdf_stream(content_or_path, min_size=min_size, allow_print_fallback=allow_print_fallback)
        return True
    except InvalidPDFError:
        return False


DEFAULT_DESKTOP_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
STEALTH_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-infobars",
    "--disable-dev-shm-usage"
]
DEFAULT_PROFILE_DIR = ROOT / "state" / "chromium_profile"

ANTI_BOT_PROMPT = "Automated retrieval blocked by anti-bot challenge. Please complete the challenge or download the PDF manually in the browser, then press Enter to resume..."
MANUAL_DROPIN_PROMPT = "Please download the PDF manually in your regular browser, save it as {doi_filename}.pdf inside your downloads/ folder, and press Enter."


def get_storage_state_cookies(session_file=None):
    """Load cookies from storage state JSON file."""
    s_file = session_file or auth_setup.get_existing_state_path()
    if s_file and Path(s_file).is_file():
        try:
            with open(s_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("cookies", [])
        except Exception as e:
            print(f"  Note loading cookies from {Path(s_file).name}: {e}")
    return []


def inject_storage_state_cookies(context, session_file=None):
    """Explicitly inject saved cookies from storage_state.json into the Playwright context cookie store."""
    cookies = get_storage_state_cookies(session_file)
    if cookies and hasattr(context, "add_cookies"):
        try:
            cleaned_cookies = []
            for c in cookies:
                c_copy = dict(c)
                if c_copy.get("sameSite") not in ("Strict", "Lax", "None"):
                    c_copy.pop("sameSite", None)
                cleaned_cookies.append(c_copy)
            context.add_cookies(cleaned_cookies)
        except Exception as e:
            print(f"  Note injecting cookies into browser context: {e}")
    return cookies


def get_cookie_header_string(session_file=None):
    """Format cookies from storage_state.json as an HTTP Cookie header string."""
    cookies = get_storage_state_cookies(session_file)
    cookie_parts = []
    for c in cookies:
        if "name" in c and "value" in c:
            cookie_parts.append(f"{c['name']}={c['value']}")
    return "; ".join(cookie_parts) if cookie_parts else ""


def log_failure(doi, failure_stage, error, endpoint_url=None, working_dir=None):
    """Two-tier error logging system:
    1. working/error_audit.jsonl: Detailed JSON lines log capturing timestamp, DOI, failure stage, error type, and endpoint URL.
    2. working/failed_papers.log: Clean, deduplicated summary log. Checks if DOI is already logged so repeat runs do not flood the file with redundant entries.
    """
    clean_doi = normalize_doi(doi) if doi else "unknown"
    w_dir = Path(working_dir or ROOT / "working").resolve()
    w_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).isoformat()
    error_type = type(error).__name__ if isinstance(error, BaseException) else str(error)
    error_msg = str(error) if isinstance(error, BaseException) else ""

    # Tier 1: Detailed JSON lines audit log
    audit_file = w_dir / "error_audit.jsonl"
    audit_entry = {
        "timestamp": timestamp,
        "doi": clean_doi,
        "failure_stage": failure_stage,
        "error_type": error_type,
        "endpoint_url": endpoint_url or "",
        "error_message": error_msg,
    }
    try:
        with open(audit_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"  Note: Failed writing to {audit_file.name}: {e}")

    # Tier 2: Clean, deduplicated summary log
    summary_file = w_dir / "failed_papers.log"
    already_logged = False
    if summary_file.is_file():
        try:
            content = summary_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if clean_doi.lower() in line.lower():
                    already_logged = True
                    break
        except Exception:
            already_logged = False

    if not already_logged:
        summary_line = f"[{timestamp}] DOI: {clean_doi} | Stage: {failure_stage} | Error: {error_type} - {error_msg}\n"
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write(summary_line)
            print(f"Logged failure to {summary_file.name} (DOI: {clean_doi})")
        except Exception as e:
            print(f"  Note: Failed writing to {summary_file.name}: {e}")
    else:
        print(f"DOI {clean_doi} already logged in {summary_file.name}; skipping duplicate summary entry.")


def log_manual_pdf_url(doi, title=None, target_article_url=None, working_dir=None):
    """Record a paper that fell back to print-to-PDF into dedicated manual download queue files:
    1. working/manual_pdf_urls.json: Structured JSON array of entries.
    2. working/manual_pdf_urls.txt: Plaintext line log.
    Deduplicated so repeat runs do not create duplicate entries.
    """
    clean_doi = normalize_doi(doi) if doi else "unknown"
    w_dir = Path(working_dir or ROOT / "working").resolve()
    w_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).isoformat()
    clean_title = (title or f"Paper {clean_doi}").replace(PRINT_CAPTURE_TAG, "").strip()
    target_url = target_article_url or (f"https://doi.org/{clean_doi}" if clean_doi != "unknown" else "")

    # 1. JSON queue log (deduplicated by DOI)
    json_path = w_dir / MANUAL_PDF_URLS_JSON
    entries = []
    found_idx = -1
    if json_path.is_file():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    entries = data
                    for i, item in enumerate(entries):
                        if isinstance(item, dict) and normalize_doi(item.get("doi")) == clean_doi:
                            found_idx = i
                            break
        except Exception as e:
            print(f"  Note reading {json_path.name}: {e}")

    if found_idx == -1:
        entries.append({
            "doi": clean_doi,
            "title": clean_title,
            "target_article_url": target_url,
            "timestamp": timestamp,
        })
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, ensure_ascii=False)
            print(f"Recorded paper to manual download queue in {json_path.name} (DOI: {clean_doi})")
        except Exception as e:
            print(f"  Note writing to {json_path.name}: {e}")
    else:
        if clean_title and clean_title != f"Paper {clean_doi}" and entries[found_idx].get("title") == f"Paper {clean_doi}":
            entries[found_idx]["title"] = clean_title
            if target_url:
                entries[found_idx]["target_article_url"] = target_url
            try:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(entries, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
        print(f"DOI {clean_doi} already present in {json_path.name}; skipping duplicate JSON entry.")

    # 2. Text queue log (deduplicated by DOI)
    txt_path = w_dir / MANUAL_PDF_URLS_TXT
    already_in_txt = False
    if txt_path.is_file():
        try:
            content = txt_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                if clean_doi.lower() in line.lower():
                    already_in_txt = True
                    break
        except Exception:
            already_in_txt = False

    if not already_in_txt:
        txt_line = f"[{timestamp}] DOI: {clean_doi} | Title: {clean_title} | URL: {target_url}\n"
        try:
            with open(txt_path, "a", encoding="utf-8") as f:
                f.write(txt_line)
            print(f"Recorded paper to manual download queue in {txt_path.name} (DOI: {clean_doi})")
        except Exception as e:
            print(f"  Note writing to {txt_path.name}: {e}")
    else:
        print(f"DOI {clean_doi} already present in {txt_path.name}; skipping duplicate text entry.")


def render_print_to_pdf_fallback(page, target_article_url, pdf_path, institution=None):
    """Render article landing page in Playwright and export to PDF via page.pdf().

    Used when direct PDF download fails or returns an HTML challenge/error page
    (such as Cloudflare/anti-bot challenges or ScienceDirect/Elsevier error 600010).
    """
    pdf_path = Path(pdf_path).resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n⚡ Activating Automated Print-to-PDF Fallback (page.pdf())")
    print(f" Target Article URL: {target_article_url}")

    if not hasattr(page, "pdf"):
        print("  Playwright page object does not support pdf(); skipping print fallback.")
        return None, None

    # Ensure page is navigated to the article landing page (not the failed /pdfft endpoint)
    current_url = getattr(page, "url", "")
    needs_nav = True
    if current_url:
        if "/pdfft" not in current_url and ("article" in current_url or "doi.org" in current_url or "sciencedirect" in current_url):
            needs_nav = False

    if needs_nav and target_article_url:
        print(f"Navigating browser to article page for print capture: {target_article_url}")
        try:
            page.goto(target_article_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
        except Exception as nav_err:
            print(f"  Note during article page navigation for print capture: {nav_err}")

    try:
        pdf_bytes = page.pdf(print_background=True)
        if isinstance(pdf_bytes, (bytes, bytearray)) and pdf_bytes.startswith(b"%PDF-"):
            pdf_path.write_bytes(pdf_bytes)
            validate_pdf_stream(pdf_path, allow_print_fallback=True)
            rep, _ = source_files.inspect_file(pdf_path, expected="pdf")
            rep["is_print_fallback"] = True
            rep["print_capture"] = True
            rep["print_capture_tag"] = PRINT_CAPTURE_TAG
            print(f"✓ Print-to-PDF capture successful: {pdf_path.name} ({len(pdf_bytes):,} bytes).")
            return pdf_path, rep
        elif pdf_path.is_file() and is_valid_pdf_stream(pdf_path, allow_print_fallback=True):
            rep, _ = source_files.inspect_file(pdf_path, expected="pdf")
            rep["is_print_fallback"] = True
            rep["print_capture"] = True
            rep["print_capture_tag"] = PRINT_CAPTURE_TAG
            return pdf_path, rep
    except Exception as pdf_err:
        print(f"  Automated print-to-PDF capture encountered an error: {pdf_err}")

    return None, None


def launch_and_render_print_to_pdf(target_article_url, pdf_path, session_file=None, institution=None):
    """Launch headless browser and render target article URL to PDF via page.pdf()."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=list(STEALTH_BROWSER_ARGS)
            )
            context_kwargs = {
                "accept_downloads": True,
                "user_agent": DEFAULT_DESKTOP_USER_AGENT,
            }
            s_file = session_file or auth_setup.get_existing_state_path()
            if s_file and Path(s_file).is_file():
                context_kwargs["storage_state"] = str(s_file)

            context = browser.new_context(**context_kwargs)
            inject_storage_state_cookies(context, s_file)
            cookie_header = get_cookie_header_string(s_file)
            if cookie_header:
                try:
                    context.set_extra_http_headers({"Cookie": cookie_header})
                except Exception:
                    pass
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = context.new_page()

            res_pdf, rep = render_print_to_pdf_fallback(page, target_article_url, pdf_path, institution=institution)
            browser.close()
            return res_pdf, rep
    except Exception as e:
        print(f"  Note during launch_and_render_print_to_pdf: {e}")
        return None, None


SCIENCEDIRECT_PDF_SELECTORS = [
    "#pdf-download-link",
    "a.pdf-download-link",
    "[data-aa-name='pdf-download']",
    "a[data-aa-name='pdf-download']",
    "button[data-aa-name='pdf-download']",
    "div.pdf-link a",
    "div.pdf-link",
    "a[href*='/pdfft?']",
    "a[href*='download=true']",
    "a[href*='/pdfft']",
    "a:has-text('View PDF')",
    "a:has-text('Download PDF')",
    "button:has-text('View PDF')",
    "button:has-text('Download PDF')",
    "a.link-button:has-text('PDF')",
    "a[aria-label*='View PDF']",
    "a[aria-label*='Download PDF']",
    "a.article-pdf-link",
]

MAJOR_PUBLISHER_PDF_SELECTORS = [
    "a[data-article-pdf='true']",
    "a.pdf-download-btn",
    "a.c-pdf-download__link",
    "a[data-test='pdf-link']",
    "a.article-pdfLink",
    "a[href*='pdfdirect']",
    "a[href*='/doi/pdf/']",
    "a[href*='/doi/epdf/']",
    "a.show-pdf",
    "a:has-text('Download PDF')",
    "a:has-text('View PDF')",
    "a:has-text('Download full text in PDF')",
    "a:has-text('PDF')",
    "a[href$='.pdf']",
    "a[href*='.pdf?']",
]


def normalize_doi(doi_str):
    """Normalize a DOI string to standard 10.xxxx/... format."""
    if not doi_str:
        return ""
    val = str(doi_str).strip()
    if val.lower().startswith(("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/")):
        val = unquote(urlsplit(val).path.lstrip("/"))
    val = re.sub(r"^doi\s*:\s*", "", val, flags=re.I).strip()
    return val


def doi_to_filename(doi_str):
    """Convert a DOI to a filesystem-safe base name."""
    clean = normalize_doi(doi_str)
    return clean.replace("/", "_").replace(":", "_")


def parse_ris_file(ris_path):
    """Parse one or more RIS citation records from an .ris file."""
    path = Path(ris_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"RIS file not found: {path}")

    text = path.read_text(encoding="utf-8-sig")
    records = []
    current = {
        "authors": [],
        "raw_lines": []
    }

    tag_re = re.compile(r"^([A-Z0-9]{2})\s*-\s*(.*)$")

    for line in text.splitlines():
        line_clean = line.strip()
        if not line_clean:
            continue
        current["raw_lines"].append(line)
        match = tag_re.match(line_clean)
        if not match:
            continue
        tag, val = match.group(1), match.group(2).strip()
        if tag == "TY":
            current["type"] = val
        elif tag == "ID":
            current["id"] = val
        elif tag in ("TI", "T1"):
            current["title"] = val
        elif tag in ("AU", "A1"):
            current["authors"].append(val)
        elif tag in ("JO", "JF", "JA"):
            current["venue"] = val
        elif tag in ("PY", "Y1"):
            current["year"] = val[:4]
        elif tag == "VL":
            current["volume"] = val
        elif tag == "IS":
            current["issue"] = val
        elif tag == "SP":
            current["start_page"] = val
        elif tag == "EP":
            current["end_page"] = val
        elif tag == "DO":
            current["doi"] = normalize_doi(val)
        elif tag == "UR":
            current["url"] = val
            if not current.get("doi"):
                doi_match = re.search(r"10\.\d{4,9}/\S+", val)
                if doi_match:
                    current["doi"] = normalize_doi(doi_match.group(0))
        elif tag == "ER":
            if current.get("title") or current.get("doi"):
                records.append(current)
            current = {"authors": [], "raw_lines": []}

    if current.get("title") or current.get("doi"):
        records.append(current)

    return records


def build_ris_string(metadata):
    """Generate a clean standard RIS block from metadata."""
    lines = [
        "TY  - JOUR",
        f"ID  - {metadata.get('id', 'P0001')}",
        f"TI  - {metadata.get('title', 'Untitled')}",
    ]
    for author in metadata.get("authors", []):
        if isinstance(author, dict):
            name = author.get("literal") or f"{author.get('family', '')}, {author.get('given', '')}".strip(", ")
        else:
            name = str(author)
        if name:
            lines.append(f"AU  - {name}")

    if metadata.get("venue"):
        lines.append(f"JO  - {metadata['venue']}")
    if metadata.get("year"):
        lines.append(f"PY  - {metadata['year']}")
    if metadata.get("volume"):
        lines.append(f"VL  - {metadata['volume']}")
    if metadata.get("issue"):
        lines.append(f"IS  - {metadata['issue']}")
    if metadata.get("start_page"):
        lines.append(f"SP  - {metadata['start_page']}")
    if metadata.get("end_page"):
        lines.append(f"EP  - {metadata['end_page']}")

    doi = metadata.get("doi")
    if doi:
        lines.append(f"DO  - {doi}")
        lines.append(f"UR  - https://doi.org/{doi}")
    elif metadata.get("url"):
        lines.append(f"UR  - {metadata['url']}")

    if metadata.get("is_print_fallback"):
        lines.append(f"N1  - {PRINT_CAPTURE_TAG} Automated print-to-PDF fallback")

    lines.append("ER  - \n")
    return "\n".join(lines)


def resolve_doi_metadata(doi):
    """Attempt public bibliographic resolution via Crossref / DataCite."""
    clean = normalize_doi(doi)
    if not clean:
        return None
    try:
        import httpx
        url = f"https://api.crossref.org/works/{clean}"
        resp = httpx.get(url, timeout=12, headers={"User-Agent": "literature-research-skills/1.0 (mailto:agent@local.internal)"})
        if resp.status_code == 200:
            msg = resp.json().get("message", {})
            title = msg.get("title", [""])[0] if isinstance(msg.get("title"), list) and msg.get("title") else str(msg.get("title", ""))
            venue = msg.get("container-title", [""])[0] if isinstance(msg.get("container-title"), list) and msg.get("container-title") else str(msg.get("container-title", ""))

            authors = []
            for a in msg.get("author", []):
                family = a.get("family", "")
                given = a.get("given", "")
                if family:
                    authors.append(f"{family}, {given}".strip(", "))
                elif a.get("name"):
                    authors.append(a.get("name"))

            year = ""
            date_parts = msg.get("issued", {}).get("date-parts", [[]])[0]
            if date_parts:
                year = str(date_parts[0])

            page_str = msg.get("page", "")
            start_page, end_page = "", ""
            if "-" in page_str:
                parts = page_str.split("-", 1)
                start_page, end_page = parts[0].strip(), parts[1].strip()
            elif page_str:
                start_page = page_str.strip()

            alt_ids = msg.get("alternative-id", [])
            pii = alt_ids[0] if alt_ids else ""
            if not pii:
                res_url = msg.get("resource", {}).get("primary", {}).get("URL", "")
                pii_m = re.search(r'/pii/([A-Z0-9]+)', res_url, re.I)
                if pii_m:
                    pii = pii_m.group(1)

            oa_links = []
            for item in msg.get("link", []):
                u = item.get("URL")
                ct = item.get("content-type", "").lower()
                if u and ("pdf" in ct or u.lower().endswith(".pdf")):
                    if u not in oa_links:
                        oa_links.append(u)

            return {
                "doi": clean,
                "title": title or f"Paper {clean}",
                "authors": authors,
                "year": year,
                "venue": venue,
                "volume": str(msg.get("volume", "")),
                "issue": str(msg.get("issue", "")),
                "start_page": start_page,
                "end_page": end_page,
                "pii": pii,
                "url": f"https://doi.org/{clean}",
                "oa_links": oa_links,
            }
        elif resp.status_code == 404:
            # Fallback to DataCite API for arXiv (10.48550/) and repository DOIs
            dc_url = f"https://api.datacite.org/dois/{clean}"
            dc_resp = httpx.get(dc_url, timeout=12, headers={"User-Agent": "literature-research-skills/1.0"})
            if dc_resp.status_code == 200:
                attrs = dc_resp.json().get("data", {}).get("attributes", {})
                titles = attrs.get("titles", [])
                title = titles[0].get("title", "") if titles else ""
                year = str(attrs.get("publicationYear", ""))
                venue = attrs.get("container", {}).get("title", "") or ("arXiv" if "arxiv" in clean.lower() else "")

                authors = []
                for c in attrs.get("creators", []):
                    fn = c.get("familyName", "")
                    gn = c.get("givenName", "")
                    if fn and gn:
                        authors.append(f"{fn}, {gn}")
                    elif c.get("name"):
                        authors.append(c.get("name"))

                return {
                    "doi": clean,
                    "title": title or f"Paper {clean}",
                    "authors": authors,
                    "year": year,
                    "venue": venue,
                    "volume": "",
                    "issue": "",
                    "start_page": "",
                    "end_page": "",
                    "pii": "",
                    "url": f"https://doi.org/{clean}",
                    "oa_links": [],
                }
    except Exception:
        pass
    return None


def find_paper_in_catalogue(doi):
    """Look up paper metadata in local catalogue if present."""
    papers_path = ROOT / "search-records" / "papers.jsonl"
    if not papers_path.is_file():
        return None
    try:
        clean_target = normalize_doi(doi)
        with open(papers_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                paper = json.loads(line)
                paper_doi = normalize_doi(paper.get("identifiers", {}).get("doi") or paper.get("article_url", ""))
                if paper_doi == clean_target:
                    return paper
    except Exception:
        pass
    return None


def check_local_downloads(pii=None, safe_name=None):
    """Check common local download intake directories for matching paper PDFs."""
    user_downloads = Path.home() / "Downloads"
    if not user_downloads.is_dir():
        return None

    candidates = []
    if pii:
        candidates.extend([
            user_downloads / f"PII{pii}.pdf",
            user_downloads / f"PII_{pii}.pdf",
            user_downloads / f"1-s2.0-{pii}-main.pdf",
            user_downloads / f"{pii}.pdf",
        ])
    if safe_name:
        candidates.extend([
            user_downloads / f"{safe_name}.pdf",
            user_downloads / f"{safe_name.replace(':', '_')}.pdf",
        ])

    for cand in candidates:
        if cand.is_file() and is_valid_pdf_stream(cand):
            rep, _ = source_files.inspect_file(cand, expected="pdf")
            if rep.get("mechanical_status") == "pass":
                return cand

    if pii:
        for f in user_downloads.glob(f"*{pii}*.pdf"):
            if f.is_file() and is_valid_pdf_stream(f):
                rep, _ = source_files.inspect_file(f, expected="pdf")
                if rep.get("mechanical_status") == "pass":
                    return f

    return None


# Known Open-Access publishers, repositories, and DOI prefixes
KNOWN_OA_PUBLISHERS = [
    ("eLife", ("10.7554/", "elifesciences.org")),
    ("PLOS", ("10.1371/", "journals.plos.org")),
    ("Frontiers", ("10.3389/", "frontiersin.org")),
    ("arXiv", ("10.48550/", "arxiv.org", "arxiv:")),
    ("PMC", ("pmc.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov/pmc")),
    ("BioMed Central", ("10.1186/", "biomedcentral.com")),
    ("MDPI", ("10.3390/", "mdpi.com")),
    ("PeerJ", ("10.7717/", "peerj.com")),
    ("Hindawi", ("10.1155/", "hindawi.com")),
    ("Copernicus", ("10.5194/", "copernicus.org")),
    ("Open Library of Humanities", ("10.16995/", "olh.openlibhums.org")),
]


def is_known_open_access_doi(doi_str, direct_url=None):
    """Check if a DOI or direct URL belongs to known open-access publishers or repositories."""
    target = f"{normalize_doi(doi_str)} {direct_url or ''}".lower()
    if re.search(r'\bpmc\d+\b', target, re.I):
        return True, "PMC"
    for pub_name, triggers in KNOWN_OA_PUBLISHERS:
        for trig in triggers:
            if trig.lower() in target:
                return True, pub_name
    return False, None


def extract_elife_article_id(doi_str, direct_url=None):
    """Extract numeric article ID from an eLife DOI or URL (e.g. 10.7554/eLife.09560 -> 09560)."""
    target = f"{doi_str or ''} {direct_url or ''}"
    m = re.search(r'(?:10\.7554/(?:elife\.)?|/articles/)([0-9]+)', target, re.I)
    if m:
        return m.group(1)
    return None


def query_crossref_oa_links(doi_str):
    """Query Crossref API for direct full-text PDF links."""
    clean = normalize_doi(doi_str)
    if not clean:
        return []
    links = []
    try:
        import httpx
        url = f"https://api.crossref.org/works/{clean}"
        resp = httpx.get(
            url,
            timeout=10,
            headers={"User-Agent": "literature-research-skills/1.0 (mailto:agent@local.internal)"}
        )
        if resp.status_code == 200:
            msg = resp.json().get("message", {})
            for item in msg.get("link", []):
                u = item.get("URL")
                ct = item.get("content-type", "").lower()
                if u and ("pdf" in ct or u.lower().endswith(".pdf")):
                    if u not in links:
                        links.append(u)
    except Exception:
        pass
    return links


def query_unpaywall_oa_links(doi_str, email=None):
    """Query Unpaywall API for open-access PDF links."""
    clean = normalize_doi(doi_str)
    if not clean:
        return []
    links = []
    em = email or os.environ.get("UNPAYWALL_EMAIL") or "dan@gmail.com"
    try:
        import httpx
        url = f"https://api.unpaywall.org/v2/{clean}?email={em}"
        resp = httpx.get(
            url,
            timeout=10,
            headers={"User-Agent": "literature-research-skills/1.0"}
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("is_oa"):
                best = data.get("best_oa_location") or {}
                if best.get("url_for_pdf") and best.get("url_for_pdf") not in links:
                    links.append(best["url_for_pdf"])
                for loc in data.get("oa_locations", []):
                    u = loc.get("url_for_pdf")
                    if u and u not in links:
                        links.append(u)
    except Exception:
        pass
    return links


def detect_and_resolve_oa(doi_str, direct_url=None):
    """Detect open-access status and build candidate direct PDF download URLs.

    Handles:
    - eLife (10.7554/): constructs https://elifesciences.org/articles/{article_id}.pdf,
      CDN direct versions, and queries Crossref/Unpaywall.
    - PLOS (10.1371/): printable endpoint and journal-specific endpoints.
    - Frontiers (10.3389/): direct article PDF endpoint.
    - arXiv (10.48550/ or arxiv.org): direct export/PDF endpoints.
    - PMC (PMCxxxxx): direct NCBI / Europe PMC endpoints.
    - Generic DOIs: queries Crossref & Unpaywall for OA links.
    """
    clean_doi = normalize_doi(doi_str)
    is_known, pub_name = is_known_open_access_doi(clean_doi, direct_url)

    candidate_urls = []
    seen = set()

    def add_url(u):
        if u and u.startswith("http") and u not in seen:
            seen.add(u)
            candidate_urls.append(u)

    # 1. eLife Resolver
    if pub_name == "eLife" or "10.7554/" in clean_doi or (direct_url and "elifesciences.org" in direct_url.lower()):
        pub_name = "eLife"
        art_id = extract_elife_article_id(clean_doi, direct_url)
        if art_id:
            # Crossref links first (provides the exact versioned CDN URL)
            for cu in query_crossref_oa_links(clean_doi):
                add_url(cu)
            # Canonical direct PDF endpoint (as specified)
            add_url(f"https://elifesciences.org/articles/{art_id}.pdf")
            # Direct binary CDN endpoints
            add_url(f"https://cdn.elifesciences.org/articles/{art_id}/elife-{art_id}-v2.pdf")
            add_url(f"https://cdn.elifesciences.org/articles/{art_id}/elife-{art_id}-v1.pdf")
            add_url(f"https://cdn.elifesciences.org/articles/{art_id}/elife-{art_id}.pdf")
            add_url(f"https://cdn.elifesciences.org/articles/{art_id}/elife-{art_id}-v3.pdf")

        for u in query_unpaywall_oa_links(clean_doi):
            add_url(u)

    # 2. PLOS Resolver
    elif pub_name == "PLOS" or "10.1371/" in clean_doi:
        pub_name = "PLOS"
        add_url(f"https://journals.plos.org/plosone/article/file?id={clean_doi}&type=printable")
        plos_journals = {
            "pone": "plosone",
            "pbio": "plosbiology",
            "pmed": "plosmedicine",
            "pcbi": "ploscompbiol",
            "pgen": "plosgenetics",
            "ppat": "plospathogens",
            "pntd": "plosntds",
            "pdig": "plosdigitalhealth",
            "pgph": "plosglobalpublichealth",
            "pclm": "plosclimate",
            "pwat": "ploswater",
        }
        for code, slug in plos_journals.items():
            if f"journal.{code}" in clean_doi.lower():
                add_url(f"https://journals.plos.org/{slug}/article/file?id={clean_doi}&type=printable")
                break
        for u in query_crossref_oa_links(clean_doi):
            add_url(u)
        for u in query_unpaywall_oa_links(clean_doi):
            add_url(u)

    # 3. Frontiers Resolver
    elif pub_name == "Frontiers" or "10.3389/" in clean_doi:
        pub_name = "Frontiers"
        add_url(f"https://www.frontiersin.org/articles/{clean_doi}/pdf")
        for u in query_crossref_oa_links(clean_doi):
            add_url(u)
        for u in query_unpaywall_oa_links(clean_doi):
            add_url(u)

    # 4. arXiv Resolver
    elif pub_name == "arXiv" or "10.48550/" in clean_doi or (direct_url and "arxiv.org" in direct_url.lower()):
        pub_name = "arXiv"
        m = re.search(r'(?:10\.48550/arXiv\.|arxiv\.org/(?:abs|pdf)/|arXiv:)(\d{4}\.\d{4,5}(?:v\d+)?|[a-z\-]+/\d{7})', clean_doi + " " + (direct_url or ""), re.I)
        if m:
            aid = m.group(1)
            add_url(f"https://arxiv.org/pdf/{aid}.pdf")
            add_url(f"https://export.arxiv.org/pdf/{aid}.pdf")

    # 5. PMC Resolver
    elif pub_name == "PMC" or re.search(r'\bPMC\d+\b', clean_doi + " " + (direct_url or ""), re.I):
        pub_name = "PMC"
        m = re.search(r'\b(PMC\d+)\b', clean_doi + " " + (direct_url or ""), re.I)
        if m:
            pmcid = m.group(1).upper()
            add_url(f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/")
            add_url(f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/")
            add_url(f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf")
        for u in query_crossref_oa_links(clean_doi):
            add_url(u)
        for u in query_unpaywall_oa_links(clean_doi):
            add_url(u)

    # 6. Other Known OA or Generic OA Check
    else:
        if is_known:
            if "10.1186/" in clean_doi:
                add_url(f"https://link.springer.com/content/pdf/{clean_doi}.pdf")
            elif "10.3390/" in clean_doi:
                add_url(f"https://www.mdpi.com/{clean_doi}/pdf")
            elif "10.7717/" in clean_doi:
                pid = clean_doi.split("/")[-1]
                add_url(f"https://peerj.com/articles/{pid}.pdf")

        for u in query_crossref_oa_links(clean_doi):
            add_url(u)
        for u in query_unpaywall_oa_links(clean_doi):
            add_url(u)

        if not pub_name and candidate_urls:
            pub_name = "Open Access"

    is_oa = bool(candidate_urls or is_known)
    return {
        "is_oa": is_oa,
        "publisher": pub_name or ("Open Access" if is_oa else None),
        "candidate_urls": candidate_urls,
    }


def download_oa_via_http(candidate_urls, pdf_path):
    """Attempt direct HTTP download of open-access PDF without proxy or browser."""
    import httpx
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/pdf,application/xhtml+xml,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    client = httpx.Client(follow_redirects=True, timeout=25.0, headers=headers)
    for url in candidate_urls:
        print(f"Probing direct Open-Access URL via HTTP: {url[:80]}...")
        try:
            resp = client.get(url)
            if resp.status_code == 200:
                body = resp.content
                if is_valid_pdf_stream(body):
                    pdf_path.write_bytes(body)
                    rep, _ = source_files.inspect_file(pdf_path, expected="pdf")
                    if rep.get("mechanical_status") == "pass":
                        print(f"✓ Retrieved valid PDF via direct HTTP ({len(body):,} bytes).")
                        return True
                    else:
                        pdf_path.unlink(missing_ok=True)
                else:
                    if b"<html" in body[:200].lower():
                        print(f"  Note: URL returned HTML content ({len(body)} bytes).")
        except Exception as e:
            print(f"  HTTP probe note: {e}")
    return False


def download_oa_via_unauthenticated_browser(candidate_urls, pdf_path, headless=True):
    """Attempt unauthenticated browser download of open-access PDF (skipping EZproxy and SSO)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed; skipping browser retrieval fallback.")
        return False

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=list(STEALTH_BROWSER_ARGS)
        )
        context = browser.new_context(
            accept_downloads=True,
            user_agent=DEFAULT_DESKTOP_USER_AGENT,
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        pdf_bytes = None
        intercepted_pdfs = []

        def on_response(res):
            ct = res.headers.get("content-type", "").lower()
            if ("application/pdf" in ct or res.url.lower().endswith(".pdf")) and res.status == 200:
                try:
                    b = res.body()
                    if is_valid_pdf_stream(b):
                        intercepted_pdfs.append(b)
                except Exception:
                    pass

        context.on("response", on_response)
        page.on("response", on_response)

        for url in candidate_urls:
            print(f"Navigating unauthenticated browser to OA URL: {url[:80]}...")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)

                # 1. Direct page content type
                content_type = page.evaluate("() => document.contentType")
                if content_type == "application/pdf":
                    try:
                        resp = context.request.get(page.url, timeout=20000)
                        if resp.status == 200 and is_valid_pdf_stream(resp.body()):
                            pdf_bytes = resp.body()
                            break
                    except Exception:
                        pass

                # 2. Intercepted response
                if intercepted_pdfs:
                    pdf_bytes = intercepted_pdfs[0]
                    break

                # 3. Embedded viewer frames
                for sel in ["iframe[src*='pdf']", "embed[type='application/pdf']", "object[type='application/pdf']", "iframe#pdf-js-viewer"]:
                    el = page.query_selector(sel)
                    if el:
                        src = el.get_attribute("src") or el.get_attribute("data")
                        if src and src.startswith("http"):
                            try:
                                resp = context.request.get(src, timeout=20000)
                                if resp.status == 200 and is_valid_pdf_stream(resp.body()):
                                    pdf_bytes = resp.body()
                                    break
                            except Exception:
                                pass
                if pdf_bytes:
                    break

                # 4. On-page download links/buttons
                for sel in MAJOR_PUBLISHER_PDF_SELECTORS:
                    btn = page.query_selector(sel)
                    if btn:
                        href = btn.get_attribute("href")
                        if href:
                            resolved_href = href if href.startswith("http") else urljoin(page.url, href)
                            try:
                                resp = context.request.get(resolved_href, timeout=20000)
                                if resp.status == 200 and is_valid_pdf_stream(resp.body()):
                                    pdf_bytes = resp.body()
                                    break
                            except Exception:
                                pass
                        try:
                            with page.expect_download(timeout=8000) as download_info:
                                btn.click()
                            download = download_info.value
                            download.save_as(str(pdf_path))
                            if is_valid_pdf_stream(pdf_path):
                                rep, _ = source_files.inspect_file(pdf_path, expected="pdf")
                                if rep.get("mechanical_status") == "pass":
                                    print("✓ PDF downloaded via browser download trigger.")
                                    browser.close()
                                    return True
                            pdf_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                if pdf_bytes:
                    break

            except Exception as e:
                print(f"  Browser navigation note: {e}")

        browser.close()

        if pdf_bytes and is_valid_pdf_stream(pdf_bytes):
            pdf_path.write_bytes(pdf_bytes)
            rep, _ = source_files.inspect_file(pdf_path, expected="pdf")
            if rep.get("mechanical_status") == "pass":
                return True
            else:
                pdf_path.unlink(missing_ok=True)

        return False


def interactive_headed_download(doi, target_url, pdf_path, session_file=None, pii=None, safe_name=None, institution=None, profile_dir=None):
    """Fallback for single-DOI mode: open publisher page in visible browser window for manual/challenge resolution with persistent context."""
    print("\n" + "=" * 70)
    print(" 🖥️  Activating Interactive Headed Fallback (Single-DOI Mode)")
    print(f" Target: {target_url}")
    print("=" * 70)

    auth_setup.load_env_vars()
    s_file = session_file or auth_setup.get_existing_state_path()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright is required for interactive headed fallback.")

    clean_doi = normalize_doi(doi)
    s_name = safe_name or doi_to_filename(clean_doi)
    pdf_path = Path(pdf_path).resolve()
    download_dir = pdf_path.parent
    download_dir.mkdir(parents=True, exist_ok=True)
    html_fallback_detected = False

    p_dir = Path(profile_dir or os.environ.get("CHROMIUM_PROFILE_DIR") or DEFAULT_PROFILE_DIR).resolve()
    p_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = None
        browser = None
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--disable-dev-shm-usage"
        ]

        if hasattr(p.chromium, "launch_persistent_context"):
            try:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(p_dir),
                    headless=False,
                    args=launch_args,
                    user_agent=DEFAULT_DESKTOP_USER_AGENT,
                    accept_downloads=True,
                )
            except Exception as e:
                print(f"  Note launching persistent context: {e}")

        if context is None:
            browser = p.chromium.launch(
                headless=False,
                args=launch_args
            )
            context_kwargs = {
                "accept_downloads": True,
                "user_agent": DEFAULT_DESKTOP_USER_AGENT,
            }
            if s_file and Path(s_file).is_file():
                context_kwargs["storage_state"] = str(s_file)

            context = browser.new_context(**context_kwargs)

        # Inject saved cookies from storage_state.json explicitly into cookie store and request headers
        inject_storage_state_cookies(context, s_file)
        cookie_header = get_cookie_header_string(s_file)
        if cookie_header:
            try:
                context.set_extra_http_headers({"Cookie": cookie_header})
            except Exception:
                pass

        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.pages[0] if (hasattr(context, "pages") and context.pages) else context.new_page()

        # Listen for downloads triggered by user clicks in headed browser
        def on_browser_download(download):
            try:
                print(f"Intercepted browser download: {download.suggested_filename}")
                download.save_as(str(pdf_path))
            except Exception as e:
                print(f"  Note saving intercepted download: {e}")

        page.on("download", on_browser_download)

        print(f"Navigating visible browser to publisher page: {target_url}...")
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"  Browser navigation notice: {e}")

        try:
            page_content = page.content()
            prefix = page_content[:500].strip().lower()
            if "<html" in prefix or "<!doctype html" in prefix:
                html_fallback_detected = True
        except Exception:
            pass

        # Prompt the user
        try:
            input(f"\n{ANTI_BOT_PROMPT}\n")
        except (EOFError, KeyboardInterrupt):
            print("\nResuming pipeline...")

        # If user completed challenge in browser without triggering a manual download,
        # attempt to grab the PDF stream or trigger download now that cookies/session are active
        if not (pdf_path.is_file() and is_valid_pdf_stream(pdf_path)):
            try:
                # 1. Check if current browser page is already displaying a PDF stream
                content_type = page.evaluate("() => document.contentType")
                page_u = page.url if isinstance(getattr(page, "url", None), str) else ""
                if content_type == "application/pdf" and page_u:
                    resp = context.request.get(page_u, timeout=20000)
                    if resp.status == 200 and is_valid_pdf_stream(resp.body()):
                        pdf_path.write_bytes(resp.body())
            except Exception:
                pass

        if not (pdf_path.is_file() and is_valid_pdf_stream(pdf_path)):
            # 2. If ScienceDirect, try navigating directly to pdfft download endpoint
            resolved_pii = pii
            page_u = page.url if isinstance(getattr(page, "url", None), str) else ""
            if not resolved_pii and page_u:
                pii_m = re.search(r'/article/pii/([A-Z0-9]+)', page_u, re.I) or re.search(r'/pii/([A-Z0-9]+)', page_u, re.I)
                if pii_m:
                    resolved_pii = pii_m.group(1)
            if resolved_pii:
                sd_direct_url = f"https://www.sciencedirect.com/science/article/pii/{resolved_pii}/pdfft?isDTMRedir=true&download=true"
                try:
                    with page.expect_download(timeout=15000) as dl_info:
                        try:
                            page.goto(sd_direct_url, timeout=15000)
                        except Exception:
                            pass
                    dl = dl_info.value
                    dl.save_as(str(pdf_path))
                except Exception:
                    pass

        if not (pdf_path.is_file() and is_valid_pdf_stream(pdf_path)):
            # 3. Check standard publisher download button selectors
            for sel in MAJOR_PUBLISHER_PDF_SELECTORS + SCIENCEDIRECT_PDF_SELECTORS:
                try:
                    btn = page.query_selector(sel)
                    if btn:
                        with page.expect_download(timeout=10000) as dl_info:
                            btn.click()
                        dl = dl_info.value
                        dl.save_as(str(pdf_path))
                        if pdf_path.is_file() and is_valid_pdf_stream(pdf_path):
                            break
                except Exception:
                    pass

        # Save session state if cookies were updated
        if s_file:
            try:
                context.storage_state(path=str(s_file))
            except Exception:
                pass

        if browser:
            browser.close()
        elif context:
            context.close()

    def check_for_valid_pdf():
        # 1. Check direct pdf_path
        if pdf_path.is_file():
            if is_valid_pdf_stream(pdf_path):
                rep, _ = source_files.inspect_file(pdf_path, expected="pdf")
                if rep.get("mechanical_status") == "pass":
                    return pdf_path, rep
            else:
                try:
                    b = pdf_path.read_bytes()
                    if b"<html" in b[:500].lower() or b"<!doctype html" in b[:500].lower():
                        nonlocal html_fallback_detected
                        html_fallback_detected = True
                except Exception:
                    pass
                pdf_path.unlink(missing_ok=True)

        # 2. Check local user intake (~/Downloads)
        local_intake = check_local_downloads(pii=pii, safe_name=s_name)
        if local_intake and is_valid_pdf_stream(local_intake):
            print(f"✓ Detected matching publisher PDF in user downloads: {local_intake.name} ({local_intake.stat().st_size:,} bytes)")
            shutil.copy2(local_intake, pdf_path)
            rep, _ = source_files.inspect_file(pdf_path, expected="pdf")
            if rep.get("mechanical_status") == "pass":
                return pdf_path, rep
            else:
                pdf_path.unlink(missing_ok=True)

        # 3. Check for any freshly created valid PDF in download_dir
        for cand in sorted(download_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True):
            if cand != pdf_path and cand.is_file() and is_valid_pdf_stream(cand):
                shutil.copy2(cand, pdf_path)
                rep, _ = source_files.inspect_file(pdf_path, expected="pdf")
                if rep.get("mechanical_status") == "pass":
                    return pdf_path, rep
                else:
                    pdf_path.unlink(missing_ok=True)

        return None, None

    # Initial check after interactive browser session
    found_pdf, report = check_for_valid_pdf()
    if found_pdf and report:
        print(f"✓ Verified downloaded PDF in {found_pdf.name} ({report['bytes']:,} bytes, {report.get('page_count')} pages).")
        return found_pdf, report

    # 2. Graceful Manual Drop-in Fallback:
    # If the interactive fallback still encounters a publisher block, display a clear terminal instruction telling the user:
    # "Please download the PDF manually in your regular browser, save it as {doi_filename}.pdf inside your downloads/ folder, and press Enter."
    manual_instruction = MANUAL_DROPIN_PROMPT.format(doi_filename=s_name)
    print("\n" + "!" * 70)
    print(" ⚠️  Publisher block persists after browser session.")
    print(f" {manual_instruction}")
    print("!" * 70)
    try:
        input(f"\n{manual_instruction}\n")
    except (EOFError, KeyboardInterrupt):
        print("\nResuming pipeline...")

    # Re-check downloads/ after user manual drop-in confirmation
    found_pdf, report = check_for_valid_pdf()
    if found_pdf and report:
        print(f"✓ Detected and verified manually placed PDF in {found_pdf.name} ({report['bytes']:,} bytes, {report.get('page_count')} pages).")
        return found_pdf, report

    if html_fallback_detected:
        err = InvalidPDFError(
            "Automated retrieval blocked by anti-bot challenge: Publisher page returned HTML fallback or challenge screen."
        )
        err.endpoint_url = target_url
        raise err

    err = InvalidPDFError(
        f"Automated retrieval blocked by anti-bot challenge. Failed to find valid binary PDF (> 100 KB) for DOI {clean_doi} in {download_dir} after interactive session."
    )
    err.endpoint_url = target_url
    raise err


def step_a_download_paper(doi, institution=None, state_path=None, output_dir=None, direct_url=None, pii=None, headless=True, skip_oa=False, interactive_fallback=True, is_batch=False, working_dir=None):
    """Step A: Download raw paper PDF using Open-Access direct fetch or authenticated institutional session."""
    auth_setup.load_env_vars()
    clean_doi = normalize_doi(doi)
    safe_name = doi_to_filename(clean_doi)
    default_dir = os.environ.get("PAPER_DOWNLOAD_DIR") or "downloads"
    target_out = output_dir or (Path(default_dir).expanduser() if Path(default_dir).is_absolute() else ROOT / default_dir)
    out_dir = Path(target_out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"{safe_name}.pdf"

    # Check if already present and valid in downloads/
    if pdf_path.is_file() and is_valid_pdf_stream(pdf_path, allow_print_fallback=True):
        inspect_report, _ = source_files.inspect_file(pdf_path, expected="pdf")
        if inspect_report.get("mechanical_status") in ("pass", "unresolved") and inspect_report.get("errors") == []:
            print(f"✓ Valid PDF already exists at {pdf_path} ({pdf_path.stat().st_size:,} bytes).")
            return pdf_path, inspect_report

    # Check local user intake (~/Downloads)
    local_intake = check_local_downloads(pii=pii, safe_name=safe_name)
    if local_intake and is_valid_pdf_stream(local_intake):
        print(f"✓ Detected matching publisher PDF in user downloads: {local_intake.name} ({local_intake.stat().st_size:,} bytes)")
        shutil.copy2(local_intake, pdf_path)
        inspect_report, _ = source_files.inspect_file(pdf_path, expected="pdf")
        if inspect_report.get("mechanical_status") == "pass":
            print(f"✓ Verified and imported publisher PDF to {pdf_path}")
            return pdf_path, inspect_report

    # Detect and handle Open Access (bypasses EZproxy and Playwright SSO)
    if not skip_oa:
        oa_info = detect_and_resolve_oa(clean_doi, direct_url=direct_url)
        if oa_info.get("is_oa") and oa_info.get("candidate_urls"):
            pub = oa_info.get("publisher") or "Open Access"
            print(f"⚡ Detected Open-Access target ({pub}). Resolving directly without EZproxy...")

            # Strategy 1: Direct HTTP download
            if download_oa_via_http(oa_info["candidate_urls"], pdf_path):
                report, _ = source_files.inspect_file(pdf_path, expected="pdf")
                if report.get("mechanical_status") == "pass":
                    print(f"✓ Step A Complete (OA Direct HTTP): Verified PDF saved to {pdf_path} ({report['bytes']:,} bytes, {report.get('page_count')} pages). Bypassed EZproxy.")
                    return pdf_path, report
                else:
                    pdf_path.unlink(missing_ok=True)

            # Strategy 2: Unauthenticated browser fetch
            print(f"Attempting unauthenticated browser fetch for {pub} OA paper...")
            if download_oa_via_unauthenticated_browser(oa_info["candidate_urls"], pdf_path, headless=headless):
                report, _ = source_files.inspect_file(pdf_path, expected="pdf")
                if report.get("mechanical_status") == "pass":
                    print(f"✓ Step A Complete (OA Unauthenticated Browser): Verified PDF saved to {pdf_path} ({report['bytes']:,} bytes, {report.get('page_count')} pages). Bypassed EZproxy.")
                    return pdf_path, report
                else:
                    pdf_path.unlink(missing_ok=True)

            print(f"Note: Direct OA retrieval did not yield a valid PDF; falling back to institutional proxy route if available.")

    session_file = auth_setup.get_existing_state_path(state_path)
    if not session_file:
        raise RuntimeError(
            f"No authenticated institutional session found at {state_path or ROOT / 'state/storage_state.json'}.\n"
            "Please run 'python3 scripts/auth_setup.py' to authenticate with your university library SSO first."
        )

    print(f"Connecting with session state from {session_file.name}...")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright is required for browser retrieval. Run: pip install playwright")

    # Detect ScienceDirect / Elsevier DOI (10.1016/)
    is_sciencedirect = "10.1016/" in clean_doi or (direct_url and "sciencedirect" in direct_url.lower())

    # Construct candidate download URLs
    target_article_url = direct_url or f"https://doi.org/{clean_doi}"
    proxied_article_url = auth_setup.get_proxied_url(target_article_url, institution)

    # Publisher direct heuristics
    direct_candidates = []
    if is_sciencedirect:
        print("⚡ Detected ScienceDirect / Elsevier DOI. Activating specialized resolver...")
        sd_pii = pii
        if not sd_pii and direct_url and "/pii/" in direct_url:
            m = re.search(r'/pii/([A-Z0-9]+)', direct_url, re.I)
            if m:
                sd_pii = m.group(1)
        if not sd_pii:
            res_meta = resolve_doi_metadata(clean_doi)
            if res_meta and res_meta.get("pii"):
                sd_pii = res_meta["pii"]

        if sd_pii:
            sd_canonical = f"https://www.sciencedirect.com/science/article/pii/{sd_pii}"
            proxied_article_url = auth_setup.get_proxied_url(sd_canonical, institution)
            print(f"Targeting canonical ScienceDirect PII endpoint: {sd_pii}")

    elif "10.1002/" in clean_doi or "wiley" in proxied_article_url.lower():
        # Wiley Online Library direct binary endpoint
        wiley_direct = f"https://onlinelibrary.wiley.com/doi/pdfdirect/{clean_doi}"
        direct_candidates.append(auth_setup.get_proxied_url(wiley_direct, institution))

    elif "10.1007/" in clean_doi or "10.1038/" in clean_doi or "springer" in proxied_article_url.lower():
        springer_direct = f"https://link.springer.com/content/pdf/{clean_doi}.pdf"
        direct_candidates.append(auth_setup.get_proxied_url(springer_direct, institution))

    max_auth_attempts = 2 if (interactive_fallback and not is_batch) else 1
    current_headless = headless

    try:
        for attempt in range(max_auth_attempts):
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=current_headless,
                        args=list(STEALTH_BROWSER_ARGS)
                    )
                    context = browser.new_context(
                        storage_state=str(session_file),
                        accept_downloads=True,
                        user_agent=DEFAULT_DESKTOP_USER_AGENT,
                    )
                    # Explicit session cookie injection into cookie store and request headers
                    inject_storage_state_cookies(context, session_file)
                    cookie_header = get_cookie_header_string(session_file)
                    if cookie_header:
                        try:
                            context.set_extra_http_headers({"Cookie": cookie_header})
                        except Exception:
                            pass
                    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                    page = context.new_page()

                    pdf_bytes = None
                    intercepted_pdfs = []

                    def on_response_stream(res):
                        ct = res.headers.get("content-type", "").lower()
                        if ("application/pdf" in ct or "octet-stream" in ct or "/pdfft" in res.url.lower() or res.url.lower().endswith(".pdf")) and res.status == 200:
                            try:
                                b = res.body()
                                if is_valid_pdf_stream(b):
                                    intercepted_pdfs.append(b)
                            except Exception:
                                pass

                    context.on("response", on_response_stream)
                    page.on("response", on_response_stream)

                    # Specialized ScienceDirect / Elsevier Resolver
                    if is_sciencedirect:
                        print(f"Navigating to ScienceDirect article landing page: {proxied_article_url}")
                        page.goto(proxied_article_url, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(2000)

                        if auth_setup.is_login_or_challenge(page=page, institution=institution):
                            raise auth_setup.InstitutionalAuthChallenge(
                                message="Institutional login wall or 2FA prompt detected during ScienceDirect navigation",
                                target_url=proxied_article_url
                            )

                        # 1. Direct ScienceDirect PII Resolution:
                        # Resolve article's PII from landing page URL, meta tags, context.request query, or direct API query
                        if not sd_pii:
                            pii_m = re.search(r'/article/pii/([A-Z0-9]+)', page.url, re.I) or re.search(r'/pii/([A-Z0-9]+)', page.url, re.I)
                            if pii_m:
                                sd_pii = pii_m.group(1)
                                print(f"Resolved ScienceDirect PII from landing page URL: {sd_pii}")

                        if not sd_pii:
                            try:
                                meta_pii = page.eval_on_selector("meta[name='citation_pii']", "el => el.content")
                                if meta_pii:
                                    sd_pii = meta_pii.strip()
                                    print(f"Resolved ScienceDirect PII from meta citation_pii: {sd_pii}")
                            except Exception:
                                pass

                        if not sd_pii:
                            try:
                                meta_pii = page.eval_on_selector("meta[name='article:pii']", "el => el.content")
                                if meta_pii:
                                    sd_pii = meta_pii.strip()
                                    print(f"Resolved ScienceDirect PII from meta article:pii: {sd_pii}")
                            except Exception:
                                pass

                        if not sd_pii:
                            # Direct landing page query via Playwright authenticated request context
                            try:
                                landing_resp = context.request.get(proxied_article_url, timeout=20000)
                                if auth_setup.is_login_or_challenge(url=landing_resp.url, institution=institution):
                                    raise auth_setup.InstitutionalAuthChallenge(
                                        message="Institutional login wall detected while querying ScienceDirect landing page",
                                        target_url=proxied_article_url
                                    )
                                m = re.search(r'/article/pii/([A-Z0-9]+)', landing_resp.url, re.I) or re.search(r'/pii/([A-Z0-9]+)', landing_resp.url, re.I)
                                if m:
                                    sd_pii = m.group(1)
                                    print(f"Resolved ScienceDirect PII from landing request redirect: {sd_pii}")
                                else:
                                    text = landing_resp.text()
                                    m = (
                                        re.search(r'name=["\']citation_pii["\']\s+content=["\']([A-Z0-9]+)["\']', text, re.I) or
                                        re.search(r'content=["\']([A-Z0-9]+)["\']\s+name=["\']citation_pii["\']', text, re.I) or
                                        re.search(r'"pii"\s*:\s*"([A-Z0-9]+)"', text, re.I) or
                                        re.search(r'/article/pii/([A-Z0-9]+)', text, re.I) or
                                        re.search(r'/pii/([A-Z0-9]+)', text, re.I)
                                    )
                                    if m:
                                        sd_pii = m.group(1)
                                        print(f"Resolved ScienceDirect PII from landing request body: {sd_pii}")
                            except auth_setup.InstitutionalAuthChallenge:
                                raise
                            except Exception as e:
                                print(f"  Note on context.request PII resolution: {e}")

                        if not sd_pii:
                            # Direct API query (Crossref / DataCite metadata)
                            res_meta = resolve_doi_metadata(clean_doi)
                            if res_meta and res_meta.get("pii"):
                                sd_pii = res_meta["pii"]
                                print(f"Resolved ScienceDirect PII from API query: {sd_pii}")

                        if not sd_pii:
                            raise InvalidPDFError(f"Could not resolve ScienceDirect PII for DOI {clean_doi} from landing page or direct API query.")

                        # Check if page navigation directly loaded a PDF stream
                        try:
                            content_type = page.evaluate("() => document.contentType")
                            if content_type == "application/pdf":
                                resp = context.request.get(page.url, timeout=25000)
                                if resp.status == 200 and resp.body().startswith(b"%PDF-"):
                                    validate_pdf_stream(resp.body())
                                    pdf_bytes = resp.body()
                                    print(f"✓ Current ScienceDirect page is directly a PDF stream ({len(pdf_bytes):,} bytes).")
                        except Exception:
                            pass

                        # 2. Directly fetch PDF stream from Elsevier's official direct download endpoint format:
                        # https://www.sciencedirect.com/science/article/pii/{pii}/pdfft?isDTMRedir=true&download=true
                        # Uses page.goto(endpoint_url) within active browser context and page.expect_download() to intercept stream
                        if not pdf_bytes:
                            official_pdf_url = f"https://www.sciencedirect.com/science/article/pii/{sd_pii}/pdfft?isDTMRedir=true&download=true"
                            print(f"Direct ScienceDirect PDF download targeting official endpoint: {official_pdf_url}")

                            candidate_pdf_urls = [official_pdf_url]
                            parsed_u = urlsplit(page.url)
                            base_origin = f"{parsed_u.scheme}://{parsed_u.netloc}"
                            if base_origin and "sciencedirect.com" not in parsed_u.netloc:
                                candidate_pdf_urls.append(f"{base_origin}/science/article/pii/{sd_pii}/pdfft?isDTMRedir=true&download=true")

                            proxied_candidate = auth_setup.get_proxied_url(official_pdf_url, institution)
                            if proxied_candidate not in candidate_pdf_urls:
                                candidate_pdf_urls.append(proxied_candidate)

                            html_fallback_detected = False
                            last_html_body = None

                            for target_pdf_url in candidate_pdf_urls:
                                print(f"Navigating browser to direct ScienceDirect PDF endpoint: {target_pdf_url}")
                                try:
                                    download = None
                                    try:
                                        with page.expect_download(timeout=25000) as download_info:
                                            try:
                                                page.goto(target_pdf_url, timeout=25000)
                                            except Exception:
                                                # page.goto typically raises net::ERR_ABORTED when navigation triggers a browser download
                                                pass
                                        download = download_info.value
                                    except Exception as dl_err:
                                        print(f"  Download event listener did not capture download for {target_pdf_url}: {dl_err}")

                                    raw_bytes = None
                                    if download:
                                        temp_pdf = out_dir / f".tmp_{safe_name}.pdf"
                                        try:
                                            download.save_as(str(temp_pdf))
                                            if temp_pdf.is_file():
                                                raw_bytes = temp_pdf.read_bytes()
                                        except Exception as save_err:
                                            print(f"  Download save_as error: {save_err}")
                                        finally:
                                            temp_pdf.unlink(missing_ok=True)

                                        if not raw_bytes and hasattr(download, "path"):
                                            try:
                                                dp = download.path()
                                                if dp and Path(dp).is_file():
                                                    raw_bytes = Path(dp).read_bytes()
                                            except Exception:
                                                pass

                                    if raw_bytes:
                                        prefix = raw_bytes[:500].decode("utf-8", errors="replace").strip().lower()
                                        if "<html" in prefix or "<!doctype html" in prefix:
                                            html_fallback_detected = True
                                            last_html_body = raw_bytes
                                            print(f"  Warning: Download from {target_pdf_url} returned an HTML document ({len(raw_bytes):,} bytes).")
                                            continue

                                        # Validate %PDF- magic bytes header
                                        if not raw_bytes.startswith(b"%PDF-"):
                                            raise InvalidPDFError(
                                                f"ScienceDirect downloaded file missing %PDF- magic bytes header. Starting bytes: {raw_bytes[:20]!r}"
                                            )

                                        validate_pdf_stream(raw_bytes)
                                        pdf_bytes = raw_bytes
                                        print(f"✓ Intercepted and validated binary PDF stream via expect_download ({len(pdf_bytes):,} bytes).")
                                        break
                                    else:
                                        # No download captured; inspect page for institutional login challenge or HTML fallback
                                        if auth_setup.is_login_or_challenge(page=page, institution=institution):
                                            raise auth_setup.InstitutionalAuthChallenge(
                                                message="ScienceDirect direct PDF endpoint redirected to institutional login wall",
                                                target_url=target_pdf_url
                                            )

                                        try:
                                            page_html = page.content()
                                            prefix = page_html[:500].strip().lower()
                                            if "<html" in prefix or "<!doctype html" in prefix:
                                                html_fallback_detected = True
                                                last_html_body = page_html.encode("utf-8")
                                                print(f"  Warning: Navigation to {target_pdf_url} rendered an HTML fallback page.")
                                        except Exception:
                                            pass

                                except auth_setup.InstitutionalAuthChallenge:
                                    raise
                                except InvalidPDFError:
                                    raise
                                except Exception as fetch_err:
                                    print(f"  Direct PDF fetch note for {target_pdf_url}: {fetch_err}")

                            # If no valid PDF bytes retrieved and an HTML fallback page was returned, trigger automated print-to-PDF
                            if not pdf_bytes:
                                print(f"⚡ Direct ScienceDirect PDF retrieval failed or returned HTML challenge/error (e.g. error 600010).")
                                print("Activating automated print-to-PDF fallback (page.pdf())...")
                                res_pdf, rep = render_print_to_pdf_fallback(
                                    page=page,
                                    target_article_url=proxied_article_url or target_article_url,
                                    pdf_path=pdf_path,
                                    institution=institution
                                )
                                if res_pdf and rep:
                                    browser.close()
                                    return res_pdf, rep
                                elif html_fallback_detected:
                                    raise InvalidPDFError(
                                        f"ScienceDirect returned an HTML fallback page ({len(last_html_body) if last_html_body else 0:,} bytes) rather than valid PDF bytes. "
                                        f"Official endpoint: {official_pdf_url}"
                                    )

                    else:
                        # General Publisher Flow (Wiley, Springer, HighWire, Silverchair, etc.)
                        # Strategy 1: Try authenticated direct PDF endpoint
                        for candidate in direct_candidates:
                            print(f"Attempting direct PDF retrieval via {candidate[:80]}...")
                            try:
                                resp = context.request.get(candidate, timeout=25000)
                                if auth_setup.is_login_or_challenge(url=resp.url, html=resp.text() if "text/html" in resp.headers.get("content-type", "") else None, institution=institution):
                                    raise auth_setup.InstitutionalAuthChallenge(
                                        message="Direct candidate endpoint redirected to institutional login wall",
                                        target_url=candidate
                                    )
                                if resp.status == 200 and is_valid_pdf_stream(resp.body()):
                                    print(f"✓ Direct PDF endpoint returned {len(resp.body()):,} bytes.")
                                    pdf_bytes = resp.body()
                                    break
                            except auth_setup.InstitutionalAuthChallenge:
                                raise
                            except Exception as e:
                                print(f"  Direct probe note: {e}")

                        # Strategy 2: Navigate proxied page and capture viewer/iframe or download trigger
                        if not pdf_bytes:
                            print(f"Navigating to proxied article page: {proxied_article_url}")
                            page.goto(proxied_article_url, wait_until="domcontentloaded", timeout=45000)
                            page.wait_for_timeout(3000)

                            if auth_setup.is_login_or_challenge(page=page, institution=institution):
                                raise auth_setup.InstitutionalAuthChallenge(
                                    message="Institutional login wall or 2FA prompt detected during publisher navigation",
                                    target_url=proxied_article_url
                                )

                            # Check if current response or page is directly a PDF
                            try:
                                content_type = page.evaluate("() => document.contentType")
                                if content_type == "application/pdf":
                                    resp = context.request.get(page.url, timeout=25000)
                                    if resp.status == 200 and is_valid_pdf_stream(resp.body()):
                                        pdf_bytes = resp.body()
                            except Exception:
                                pass

                            # Check for embedded PDF iframe (e.g., Wiley viewer: iframe#pdf-iframe)
                            if not pdf_bytes:
                                for selector in ["iframe#pdf-iframe", "iframe[src*='pdf']", "embed[type='application/pdf']", "iframe"]:
                                    el = page.query_selector(selector)
                                    if el:
                                        src = el.get_attribute("src")
                                        if src and ("pdf" in src.lower() or "direct" in src.lower()):
                                            iframe_url = src if src.startswith("http") else urljoin(page.url, src)
                                            print(f"Found embedded PDF source: {iframe_url[:80]}...")
                                            try:
                                                resp = context.request.get(iframe_url, timeout=30000)
                                                if auth_setup.is_login_or_challenge(url=resp.url, html=resp.text() if "text/html" in resp.headers.get("content-type", "") else None, institution=institution):
                                                    raise auth_setup.InstitutionalAuthChallenge(
                                                        message="Embedded PDF frame redirected to institutional login wall",
                                                        target_url=iframe_url
                                                    )
                                                if resp.status == 200 and is_valid_pdf_stream(resp.body()):
                                                    pdf_bytes = resp.body()
                                                    break
                                            except auth_setup.InstitutionalAuthChallenge:
                                                raise
                                            except Exception as e:
                                                print(f"  Iframe fetch note: {e}")

                            # Check for candidate PDF links on the page (HighWire, Silverchair, Wiley, Springer, etc.)
                            if not pdf_bytes:
                                try:
                                    links = page.eval_on_selector_all("a[href*='pdf']", "elements => elements.map(e => e.href)")
                                    sorted_links = sorted(
                                        [l for l in links if l and l.startswith("http")],
                                        key=lambda x: (
                                            0 if ".full.pdf" in x.lower() else
                                            1 if "pdfdirect" in x.lower() else
                                            2 if x.lower().endswith(".pdf") else
                                            3 if "/pdf/" in x.lower() else 4
                                        )
                                    )
                                    for href in sorted_links:
                                        print(f"Testing candidate PDF link: {href[:80]}...")
                                        try:
                                            resp = context.request.get(href, timeout=30000)
                                            if auth_setup.is_login_or_challenge(url=resp.url, html=resp.text() if "text/html" in resp.headers.get("content-type", "") else None, institution=institution):
                                                raise auth_setup.InstitutionalAuthChallenge(
                                                    message="Candidate PDF link redirected to institutional login wall",
                                                    target_url=href
                                                )
                                            if resp.status == 200 and is_valid_pdf_stream(resp.body()):
                                                print(f"✓ Downloaded PDF via link ({len(resp.body()):,} bytes).")
                                                pdf_bytes = resp.body()
                                                break
                                        except auth_setup.InstitutionalAuthChallenge:
                                            raise
                                        except Exception as e:
                                            print(f"  Link probe note: {e}")
                                except auth_setup.InstitutionalAuthChallenge:
                                    raise
                                except Exception as e:
                                    print(f"  Link evaluation note: {e}")

                            # Check for standard PDF download buttons/links with browser download event
                            if not pdf_bytes:
                                for btn_sel in MAJOR_PUBLISHER_PDF_SELECTORS:
                                    btn = page.query_selector(btn_sel)
                                    if btn:
                                        href = btn.get_attribute("href")
                                        if href:
                                            target_href = href if href.startswith("http") else urljoin(page.url, href)
                                            print(f"Attempting download link: {target_href[:80]}...")
                                            try:
                                                resp = context.request.get(target_href, headers={"Referer": page.url}, timeout=30000)
                                                if auth_setup.is_login_or_challenge(url=resp.url, html=resp.text() if "text/html" in resp.headers.get("content-type", "") else None, institution=institution):
                                                    raise auth_setup.InstitutionalAuthChallenge(
                                                        message="Download button link redirected to institutional login wall",
                                                        target_url=target_href
                                                    )
                                                if resp.status == 200 and is_valid_pdf_stream(resp.body()):
                                                    pdf_bytes = resp.body()
                                                    break
                                            except auth_setup.InstitutionalAuthChallenge:
                                                raise
                                            except Exception:
                                                pass
                                        try:
                                            with page.expect_download(timeout=10000) as download_info:
                                                btn.click()
                                            download = download_info.value
                                            download.save_as(str(pdf_path))
                                            if pdf_path.is_file():
                                                downloaded_bytes = pdf_path.read_bytes()
                                                if is_valid_pdf_stream(downloaded_bytes):
                                                    print(f"✓ Download triggered via browser event ({len(downloaded_bytes):,} bytes).")
                                                    pdf_bytes = downloaded_bytes
                                                    break
                                                else:
                                                    pdf_path.unlink(missing_ok=True)
                                        except Exception:
                                            pass
                                        if auth_setup.is_login_or_challenge(page=page, institution=institution):
                                            raise auth_setup.InstitutionalAuthChallenge(
                                                message="Download click redirected to institutional login wall",
                                                target_url=page.url
                                            )

                    browser.close()

                if pdf_bytes:
                    validate_pdf_stream(pdf_bytes)
                    pdf_path.write_bytes(pdf_bytes)

                break

            except auth_setup.InstitutionalAuthChallenge as challenge_exc:
                if attempt < max_auth_attempts - 1:
                    print(f"\n{auth_setup.AUTH_CHALLENGE_PROMPT}\n")
                    challenge_url = challenge_exc.target_url or proxied_article_url
                    save_destination = state_path or auth_setup.DEFAULT_STATE_PATH
                    auth_setup.authenticate_interactive(
                        institution=institution,
                        target_url=challenge_url,
                        state_path=save_destination,
                        headless=False
                    )
                    session_file = auth_setup.get_existing_state_path(state_path)
                    print("✓ Fresh institutional session captured. Resuming paper retrieval pipeline...")
                    continue
                else:
                    raise

        if pdf_bytes and not pdf_path.is_file():
            validate_pdf_stream(pdf_bytes)
            pdf_path.write_bytes(pdf_bytes)

        if not pdf_path.is_file():
            print(f"⚡ Direct publisher PDF retrieval failed for DOI {clean_doi}. Activating automated print-to-PDF fallback (page.pdf())...")
            res_pdf, rep = launch_and_render_print_to_pdf(
                target_article_url=proxied_article_url if 'proxied_article_url' in locals() else (target_article_url if 'target_article_url' in locals() else f"https://doi.org/{clean_doi}"),
                pdf_path=pdf_path,
                session_file=session_file if 'session_file' in locals() else None,
                institution=institution
            )
            if res_pdf and rep:
                return res_pdf, rep
            raise InvalidPDFError(f"Failed to retrieve valid PDF for DOI {clean_doi}: No PDF file was saved.")

        # Validation & Quality Check
        # Verify that the retrieved file is a valid binary PDF stream (checking that the file magic bytes start with %PDF- and that the file size is greater than 100 KB)
        # before passing it to mechanical validation and Markdown conversion.
        # If an HTML/print fallback or invalid stub is produced, raise a clean exception rather than saving it as paper.pdf.
        try:
            validate_pdf_stream(pdf_path)
        except InvalidPDFError as val_err:
            pdf_path.unlink(missing_ok=True)
            raise InvalidPDFError(f"Validation failed for retrieved file {pdf_path.name}: {val_err}") from val_err

        # Verify PDF
        report, _ = source_files.inspect_file(pdf_path, expected="pdf")
        if report.get("mechanical_status") != "pass":
            pdf_path.unlink(missing_ok=True)
            raise RuntimeError(f"Retrieved file failed mechanical PDF verification: {report.get('errors')}")

        print(f"✓ Step A Complete: Verified PDF saved to {pdf_path} ({report['bytes']:,} bytes, {report.get('page_count')} pages).")
        return pdf_path, report
    except (InvalidPDFError, RuntimeError, auth_setup.InstitutionalAuthChallenge, Exception) as err:
        target_endpoint_url = getattr(err, "endpoint_url", None) or (
            (proxied_article_url if 'proxied_article_url' in locals() else None) or
            (target_article_url if 'target_article_url' in locals() else None) or
            direct_url or
            f"https://doi.org/{clean_doi}"
        )
        setattr(err, "endpoint_url", target_endpoint_url)

        # Attempt automated print-to-PDF recovery if valid PDF was not captured
        if not (pdf_path.is_file() and is_valid_pdf_stream(pdf_path, allow_print_fallback=True)):
            print(f"⚡ Attempting automated print-to-PDF recovery for {clean_doi} (page.pdf())...")
            try:
                res_pdf, rep = launch_and_render_print_to_pdf(
                    target_article_url=target_endpoint_url,
                    pdf_path=pdf_path,
                    session_file=session_file if 'session_file' in locals() else None,
                    institution=institution
                )
                if res_pdf and rep:
                    return res_pdf, rep
            except Exception as print_fallback_err:
                print(f"  Automated print-to-PDF recovery note: {print_fallback_err}")

        if not is_batch and interactive_fallback and headless:
            print(f"\n⚡ Headless retrieval failure on DOI {clean_doi}: {err}")
            print("Switching from headless to interactive headed mode...")
            return interactive_headed_download(
                doi=clean_doi,
                target_url=target_endpoint_url,
                pdf_path=pdf_path,
                session_file=session_file if 'session_file' in locals() else None,
                pii=sd_pii if 'sd_pii' in locals() else pii,
                safe_name=safe_name,
                institution=institution
            )
        else:
            w_dir = Path(working_dir or (output_dir or ROOT / "downloads")).resolve().parent / "working"
            log_failure(
                doi=clean_doi,
                failure_stage="step_a_download",
                error=err,
                endpoint_url=target_endpoint_url,
                working_dir=w_dir
            )
            setattr(err, "_already_logged", True)
            raise


def step_b_convert_to_markdown(pdf_path, metadata, source_text_dir=None):
    """Step B: Convert downloaded PDF into clean structured Markdown with YAML frontmatter."""
    pdf_path = Path(pdf_path).resolve()
    is_fallback = bool(metadata.get("is_print_fallback") or PRINT_CAPTURE_TAG in metadata.get("title", ""))
    validate_pdf_stream(pdf_path, allow_print_fallback=is_fallback)
    clean_doi = normalize_doi(metadata.get("doi", ""))
    safe_name = doi_to_filename(clean_doi) if clean_doi else pdf_path.stem
    out_dir = Path(source_text_dir or ROOT / "working" / "source-text").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = out_dir / f"{safe_name}.md"

    print(f"Converting {pdf_path.name} to structured Markdown...")

    markdown_body = ""
    tool_used = "none"

    if pymupdf4llm is not None:
        try:
            markdown_body = pymupdf4llm.to_markdown(str(pdf_path))
            tool_used = "pymupdf4llm"
        except Exception as e:
            print(f"  pymupdf4llm extraction note: {e}")

    if not markdown_body and fitz is not None:
        try:
            doc = fitz.open(pdf_path)
            pages_text = []
            for i, page in enumerate(doc):
                txt = page.get_text("text")
                pages_text.append(f"<!-- Page {i+1} -->\n\n{txt}")
            markdown_body = "\n\n".join(pages_text)
            tool_used = "pymupdf"
        except Exception as e:
            print(f"  PyMuPDF text extraction note: {e}")

    if not markdown_body:
        # Fallback to pypdf extraction via source_files
        report, extracted = source_files.inspect_file(pdf_path, extract_all=True)
        if extracted:
            markdown_body = extracted
            tool_used = "pypdf"
        else:
            raise RuntimeError(f"Could not extract readable text from {pdf_path}.")

    # Format authors for YAML frontmatter
    authors_yaml = []
    for author in metadata.get("authors", []):
        if isinstance(author, dict):
            name = author.get("literal") or f"{author.get('family', '')}, {author.get('given', '')}".strip(", ")
        else:
            name = str(author)
        if name:
            authors_yaml.append(f'  - "{name}"')
    authors_block = "\n".join(authors_yaml) if authors_yaml else '  - "Unknown"'

    rel_pdf = str(pdf_path.relative_to(ROOT)) if pdf_path.is_relative_to(ROOT) else str(pdf_path)

    fallback_yaml = ""
    if is_fallback:
        fallback_yaml = f"is_print_fallback: true\nprint_capture: true\nprint_capture_tag: \"{PRINT_CAPTURE_TAG}\"\n"

    frontmatter = f"""---
title: "{metadata.get('title', 'Untitled')}"
doi: "{clean_doi}"
year: {metadata.get('year', 'null')}
venue: "{metadata.get('venue', '')}"
authors:
{authors_block}
source_pdf: "{rel_pdf}"
converted_at: "{datetime.now(timezone.utc).isoformat()}"
conversion_tool: "{tool_used}"
{fallback_yaml}---

"""
    full_content = frontmatter + markdown_body.strip() + "\n"
    markdown_path.write_text(full_content, encoding="utf-8")

    lines = len(full_content.splitlines())
    chars = len(full_content)
    print(f"✓ Step B Complete: Structured Markdown saved to {markdown_path} ({chars:,} chars, {lines:,} lines).")
    return markdown_path


def resolve_zotero_collection_key(zot, collection_name):
    """Retrieve collection key by name or create a new collection if not found."""
    if not collection_name or not zot:
        return None
    target_norm = collection_name.strip().lower()
    try:
        cols = []
        if hasattr(zot, "all_collections"):
            try:
                cols = zot.all_collections()
            except Exception:
                cols = zot.collections()
        else:
            cols = zot.collections()

        if isinstance(cols, list):
            for col in cols:
                data = col.get("data", {}) if isinstance(col, dict) else {}
                name = data.get("name", "")
                if name.strip().lower() == target_norm:
                    return data.get("key") or col.get("key")

        # If not found, attempt to create it
        print(f"Collection '{collection_name}' not found; creating in Zotero library...")
        created = zot.create_collections([{"name": collection_name.strip()}])
        if created and isinstance(created, dict) and created.get("successful"):
            first_success = list(created["successful"].values())[0]
            key = first_success.get("key")
            if key:
                print(f"✓ Created Zotero collection '{collection_name}' (Key: {key}).")
                return key
    except Exception as e:
        print(f"  Note on Zotero collection resolution ({collection_name}): {e}")
    return None


def step_c_sync_or_package_zotero(pdf_path, markdown_path, metadata, zotero_dir=None, collection_name=None):
    """Step C: Sync to Zotero API if credentials exist, or export verified Zotero intake package."""
    auth_setup.load_env_vars()
    api_key = os.environ.get("ZOTERO_API_KEY")
    user_id = os.environ.get("ZOTERO_USER_ID") or os.environ.get("ZOTERO_LIBRARY_ID")
    target_collection_name = collection_name or os.environ.get("ZOTERO_COLLECTION_NAME") or "New"

    clean_doi = normalize_doi(metadata.get("doi", ""))
    safe_name = doi_to_filename(clean_doi) if clean_doi else Path(pdf_path).stem
    is_fallback = bool(metadata.get("is_print_fallback") or PRINT_CAPTURE_TAG in metadata.get("title", ""))
    validate_pdf_stream(pdf_path, allow_print_fallback=is_fallback)

    # Attempt programmatic sync if API credentials configured
    if api_key and user_id and zotero is not None:
        try:
            print(f"Syncing item to Zotero library {user_id} via PyZotero...")
            zot = zotero.Zotero(library_id=user_id, library_type="user", api_key=api_key)

            col_key = None
            if target_collection_name:
                col_key = resolve_zotero_collection_key(zot, target_collection_name)
                if col_key:
                    print(f"✓ Assigned target Zotero collection: '{target_collection_name}' (Key: {col_key})")

            template = zot.item_template("journalArticle")
            template["title"] = metadata.get("title", "Untitled")
            template["publicationTitle"] = metadata.get("venue", "")
            template["date"] = str(metadata.get("year", ""))
            template["DOI"] = clean_doi
            template["url"] = f"https://doi.org/{clean_doi}" if clean_doi else metadata.get("url", "")
            if metadata.get("volume"):
                template["volume"] = str(metadata["volume"])
            if metadata.get("issue"):
                template["issue"] = str(metadata["issue"])
            if metadata.get("start_page") or metadata.get("end_page"):
                template["pages"] = f"{metadata.get('start_page', '')}-{metadata.get('end_page', '')}".strip("-")

            if col_key:
                template["collections"] = [col_key]

            if is_fallback:
                template.setdefault("tags", []).extend([
                    {"tag": "print-capture-fallback"},
                    {"tag": "not-publisher-formatted-pdf"}
                ])

            creators = []
            for author in metadata.get("authors", []):
                if isinstance(author, dict):
                    creators.append({
                        "creatorType": "author",
                        "lastName": author.get("family", author.get("literal", "")),
                        "firstName": author.get("given", "")
                    })
                else:
                    parts = str(author).split(",", 1)
                    if len(parts) == 2:
                        creators.append({"creatorType": "author", "lastName": parts[0].strip(), "firstName": parts[1].strip()})
                    else:
                        creators.append({"creatorType": "author", "name": str(author)})
            if creators:
                template["creators"] = creators

            resp = zot.create_items([template])
            if resp.get("successful"):
                item_key = resp["successful"]["0"]["key"]
                print(f"✓ Zotero parent item created (Key: {item_key})" + (f" in collection '{target_collection_name}'." if col_key else "."))
                # Attach PDF
                try:
                    zot.attachment_simple([str(pdf_path)], item_key)
                    print("✓ Attached PDF to Zotero item.")
                except Exception as e:
                    print(f"  Note on PDF attachment: {e}")

                # Attach Markdown note
                try:
                    fallback_notice = f"<p><em>Note: This item was captured via automated print-to-PDF fallback {PRINT_CAPTURE_TAG}.</em></p>" if is_fallback else ""
                    note_body = f"<h2>{metadata.get('title')}</h2>{fallback_notice}<p>Structured Markdown generated at: <code>{markdown_path}</code></p>"
                    zot.create_notes([{"parentItem": item_key, "note": note_body}])
                    print("✓ Attached Markdown note to Zotero item.")
                except Exception as e:
                    print(f"  Note on Markdown attachment: {e}")

                return {
                    "mode": "api_synced",
                    "item_key": item_key,
                    "library_id": user_id,
                    "collection": target_collection_name if col_key else None,
                    "collection_key": col_key,
                }
        except Exception as e:
            print(f"Zotero API sync encountered an error ({e}); falling back to offline verified intake package.")

    # Fallback / Default: Verified Zotero intake package
    intake_root = Path(zotero_dir or ROOT / "working" / "zotero_intake").resolve()
    package_dir = intake_root / safe_name
    package_dir.mkdir(parents=True, exist_ok=True)

    # 1. RIS Citation File
    ris_file = package_dir / "citation.ris"
    ris_content = metadata.get("raw_ris") or build_ris_string(metadata)
    ris_file.write_text(ris_content, encoding="utf-8")

    # 2. PDF Copy
    validate_pdf_stream(pdf_path, allow_print_fallback=is_fallback)
    dest_pdf = package_dir / "paper.pdf"
    shutil.copy2(pdf_path, dest_pdf)

    # 3. Markdown Notes Copy
    dest_md = package_dir / "notes.md"
    shutil.copy2(markdown_path, dest_md)

    # 4. Intake Manifest
    tags = ["print-capture-fallback", "not-publisher-formatted-pdf"] if is_fallback else []
    manifest = {
        "paper_id": metadata.get("id", "P0001"),
        "doi": clean_doi,
        "title": metadata.get("title", ""),
        "authors": metadata.get("authors", []),
        "year": metadata.get("year", ""),
        "venue": metadata.get("venue", ""),
        "target_collection": target_collection_name,
        "intake_status": "verified",
        "is_print_fallback": is_fallback,
        "print_capture": is_fallback,
        "tags": tags,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "package_files": {
            "citation_ris": str(ris_file.relative_to(ROOT)) if ris_file.is_relative_to(ROOT) else str(ris_file),
            "paper_pdf": str(dest_pdf.relative_to(ROOT)) if dest_pdf.is_relative_to(ROOT) else str(dest_pdf),
            "notes_markdown": str(dest_md.relative_to(ROOT)) if dest_md.is_relative_to(ROOT) else str(dest_md)
        },
        "import_guide": {
            "zotero_desktop": f"1. In Zotero Desktop, open or create collection '{target_collection_name}'. 2. Click File -> Import... -> Select citation.ris. 3. Drag paper.pdf and notes.md onto the newly created citation item in '{target_collection_name}'.",
            "zotero_cloud": "Add ZOTERO_USER_ID and ZOTERO_API_KEY to your .env file to enable automated cloud upload."
        }
    }
    if is_fallback:
        manifest["capture_note"] = f"Automated print-to-PDF capture fallback {PRINT_CAPTURE_TAG}. Official publisher PDF download was blocked by challenge/error screen."
    manifest_file = package_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"✓ Step C Complete: Verified Zotero intake package generated at {package_dir}")
    print(f"  • {ris_file.name} (RIS citation)")
    print(f"  • {dest_pdf.name} (Full PDF)")
    print(f"  • {dest_md.name} (Structured Markdown)")
    print(f"  • {manifest_file.name} (Metadata & Import Guide)")
    return {
        "mode": "intake_package",
        "package_dir": str(package_dir),
        "manifest": str(manifest_file),
    }


def update_catalogue_records(doi, pdf_path, markdown_path, metadata):
    """Update search-records/papers.jsonl and review exports if catalogue is initialized."""
    papers_path = ROOT / "search-records" / "papers.jsonl"
    if not papers_path.is_file() or not catalogue:
        return

    clean_doi = normalize_doi(doi)
    papers = catalogue.load_rows(papers_path)
    target_paper = next((p for p in papers if normalize_doi(p.get("identifiers", {}).get("doi") or p.get("article_url", "")) == clean_doi), None)

    if not target_paper:
        return

    rel_pdf = str(pdf_path.relative_to(ROOT)) if pdf_path.is_relative_to(ROOT) else str(pdf_path)
    rel_md = str(markdown_path.relative_to(ROOT)) if markdown_path.is_relative_to(ROOT) else str(markdown_path)

    # Check and update source locations
    existing_paths = {loc.get("path") for loc in target_paper.get("source_locations", []) if loc.get("path")}
    if rel_pdf not in existing_paths:
        target_paper.setdefault("source_locations", []).append({
            "path": rel_pdf,
            "host": "local",
            "version": "publishedVersion",
            "format": "pdf",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "warnings": []
        })
    if rel_md not in existing_paths:
        target_paper.setdefault("source_locations", []).append({
            "path": rel_md,
            "host": "local",
            "version": "publishedVersion",
            "format": "markdown",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "warnings": []
        })

    is_fallback = bool(metadata.get("is_print_fallback") or PRINT_CAPTURE_TAG in metadata.get("title", ""))
    target_paper["download_status"] = "print_fallback_verified" if is_fallback else "pdf_verified"
    target_paper["full_text_status"] = "print_fallback_verified" if is_fallback else "verified"
    target_paper["download_verification"] = {
        "path": rel_pdf,
        "source_url": target_paper.get("download_verification", {}).get("source_url") or target_paper.get("article_url") or f"https://doi.org/{clean_doi}",
        "version": "publishedVersion",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "sha256": source_files.inspect_file(pdf_path)[0].get("sha256"),
        "page_count": source_files.inspect_file(pdf_path)[0].get("page_count"),
        "identity_checked": True,
        "readability_checked": True,
        "is_print_fallback": is_fallback,
    }
    target_paper["updated_at"] = datetime.now(timezone.utc).isoformat()

    catalogue.save_rows(papers_path, papers)
    try:
        catalogue.export_run(str(ROOT), str(ROOT / "review"), ["sources", "index", "downloads", "ris"])
        print("✓ Updated search-records/ and review/ exports.")
    except Exception as e:
        print(f"Catalogue export note: {e}")


def process_target(doi=None, ris_record=None, institution=None, state_path=None,
                   output_dir=None, source_text_dir=None, zotero_dir=None,
                   zotero_collection=None,
                   skip_download=False, skip_markdown=False, skip_zotero=False,
                   skip_oa=False, headless=True, interactive_fallback=True,
                   is_batch=False, working_dir=None):
    """Execute the end-to-end pipeline (Download -> Markdown -> Zotero) for one target."""
    inst = institution or auth_setup.get_active_institution()
    metadata = {}
    batch_mode = is_batch or bool(ris_record)
    effective_interactive_fallback = False if batch_mode else interactive_fallback
    w_dir = Path(working_dir or ROOT / "working").resolve()

    if ris_record:
        metadata.update(ris_record)
        target_doi = normalize_doi(ris_record.get("doi"))
    else:
        target_doi = normalize_doi(doi)

    if not target_doi:
        raise ValueError("A valid DOI is required for paper processing.")

    # Enrich from local catalogue if available
    cat_paper = find_paper_in_catalogue(target_doi)
    if cat_paper:
        metadata.setdefault("title", cat_paper.get("title"))
        metadata.setdefault("authors", cat_paper.get("authors"))
        metadata.setdefault("year", cat_paper.get("year"))
        metadata.setdefault("venue", cat_paper.get("venue"))
        metadata.setdefault("volume", cat_paper.get("volume"))
        metadata.setdefault("issue", cat_paper.get("issue"))
        metadata.setdefault("start_page", cat_paper.get("start_page"))
        metadata.setdefault("end_page", cat_paper.get("end_page"))
        metadata.setdefault("id", cat_paper.get("paper_id"))
    elif not ris_record:
        resolved = resolve_doi_metadata(target_doi)
        if resolved:
            metadata.setdefault("title", resolved.get("title"))
            metadata.setdefault("authors", resolved.get("authors"))
            metadata.setdefault("year", resolved.get("year"))
            metadata.setdefault("venue", resolved.get("venue"))
            metadata.setdefault("volume", resolved.get("volume"))
            metadata.setdefault("issue", resolved.get("issue"))
            metadata.setdefault("start_page", resolved.get("start_page"))
            metadata.setdefault("end_page", resolved.get("end_page"))
            metadata.setdefault("url", resolved.get("url"))
            if resolved.get("pii"):
                metadata.setdefault("pii", resolved.get("pii"))

    metadata["doi"] = target_doi
    metadata.setdefault("title", f"Paper {target_doi}")

    safe_name = doi_to_filename(target_doi)
    auth_setup.load_env_vars()
    default_dir = os.environ.get("PAPER_DOWNLOAD_DIR") or "downloads"
    target_out = output_dir or (Path(default_dir).expanduser() if Path(default_dir).is_absolute() else ROOT / default_dir)
    pdf_dest = Path(target_out).resolve() / f"{safe_name}.pdf"

    print("=" * 70)
    print(f" 🚀 Processing: {metadata.get('title')} ({target_doi})")
    oa_check = detect_and_resolve_oa(target_doi, direct_url=metadata.get("url"))
    if not skip_oa and oa_check.get("is_oa"):
        print(f"    Access Mode: Open Access ({oa_check.get('publisher')}) [Bypassing EZproxy]")
    else:
        print(f"    Institution: {inst.get('name')} [{inst.get('id')}]")
    print("=" * 70)

    # Step A: Download
    is_print_fallback = False
    try:
        if skip_download and pdf_dest.is_file():
            print(f"Step A (Download) skipped: Using existing file at {pdf_dest}")
            validate_pdf_stream(pdf_dest, allow_print_fallback=True)
            pdf_path = pdf_dest
            download_report, _ = source_files.inspect_file(pdf_path, expected="pdf")
            is_print_fallback = bool(download_report.get("is_print_fallback", False) or PRINT_CAPTURE_TAG in metadata.get("title", ""))
        else:
            pdf_path, download_report = step_a_download_paper(
                doi=target_doi,
                institution=inst,
                state_path=state_path,
                output_dir=output_dir,
                direct_url=metadata.get("url"),
                pii=metadata.get("pii"),
                headless=headless,
                skip_oa=skip_oa,
                interactive_fallback=effective_interactive_fallback,
                is_batch=batch_mode,
                working_dir=w_dir
            )
            is_print_fallback = bool(download_report.get("is_print_fallback", False))
    except Exception as exc:
        if not getattr(exc, "_already_logged", False):
            endpoint_url = getattr(exc, "endpoint_url", None) or metadata.get("url") or f"https://doi.org/{target_doi}"
            log_failure(
                doi=target_doi,
                failure_stage="step_a_download",
                error=exc,
                endpoint_url=endpoint_url,
                working_dir=w_dir
            )
            setattr(exc, "_already_logged", True)
        raise

    if is_print_fallback:
        metadata["is_print_fallback"] = True
        metadata["print_capture"] = True
        metadata["print_capture_tag"] = PRINT_CAPTURE_TAG
        if PRINT_CAPTURE_TAG not in metadata.get("title", ""):
            metadata["title"] = f"{metadata.get('title', 'Paper ' + target_doi)} {PRINT_CAPTURE_TAG}".strip()

        target_article_url = metadata.get("url") or f"https://doi.org/{target_doi}"
        log_manual_pdf_url(
            doi=target_doi,
            title=metadata["title"],
            target_article_url=target_article_url,
            working_dir=w_dir
        )

    # Step B: Markdown Conversion
    markdown_path = None
    if not skip_markdown:
        try:
            markdown_path = step_b_convert_to_markdown(
                pdf_path=pdf_path,
                metadata=metadata,
                source_text_dir=source_text_dir
            )
        except Exception as exc:
            if not getattr(exc, "_already_logged", False):
                log_failure(
                    doi=target_doi,
                    failure_stage="step_b_markdown",
                    error=exc,
                    endpoint_url=metadata.get("url") or f"https://doi.org/{target_doi}",
                    working_dir=w_dir
                )
                setattr(exc, "_already_logged", True)
            raise
    else:
        print("Step B (Markdown Conversion) skipped by user flag.")

    # Step C: Zotero Sync / Intake
    zotero_result = None
    if not skip_zotero and markdown_path:
        try:
            zotero_result = step_c_sync_or_package_zotero(
                pdf_path=pdf_path,
                markdown_path=markdown_path,
                metadata=metadata,
                zotero_dir=zotero_dir,
                collection_name=zotero_collection
            )
        except Exception as exc:
            if not getattr(exc, "_already_logged", False):
                log_failure(
                    doi=target_doi,
                    failure_stage="step_c_zotero",
                    error=exc,
                    endpoint_url=metadata.get("url") or f"https://doi.org/{target_doi}",
                    working_dir=w_dir
                )
                setattr(exc, "_already_logged", True)
            raise
    else:
        print("Step C (Zotero Sync) skipped.")

    # Catalogue Sync
    if markdown_path:
        update_catalogue_records(target_doi, pdf_path, markdown_path, metadata)

    return {
        "doi": target_doi,
        "pdf_path": str(pdf_path),
        "markdown_path": str(markdown_path) if markdown_path else None,
        "zotero": zotero_result,
        "is_print_fallback": is_print_fallback,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doi", help="Target DOI string (e.g. 10.1002/cphy.c170043)")
    parser.add_argument("--ris", help="Path to an .ris citation file")
    parser.add_argument("--output-dir", help="Directory for downloaded raw PDFs (default: $PAPER_DOWNLOAD_DIR or downloads/)")
    parser.add_argument("--source-text-dir", help="Directory for converted Markdown (default: working/source-text/)")
    parser.add_argument("--zotero-dir", help="Directory for Zotero intake packages (default: working/zotero_intake/)")
    parser.add_argument("--zotero-collection", help="Target Zotero collection name (default: $ZOTERO_COLLECTION_NAME or 'New')")
    parser.add_argument("--state-path", help="Path to Playwright storage state JSON")
    parser.add_argument("--no-headless", action="store_true", help="Launch visible browser window")
    parser.add_argument("--skip-download", action="store_true", help="Skip download if PDF already exists")
    parser.add_argument("--skip-markdown", action="store_true", help="Skip Markdown conversion")
    parser.add_argument("--skip-zotero", action="store_true", help="Skip Zotero sync/intake package")
    parser.add_argument("--skip-oa", action="store_true", help="Skip open-access auto-detection and force institutional routing")
    parser.add_argument("--no-interactive-fallback", dest="interactive_fallback", action="store_false", help="Disable interactive headed browser fallback on 2FA/SSO challenge")
    parser.set_defaults(interactive_fallback=True)
    args = parser.parse_args()

    if not args.doi and not args.ris:
        parser.error("Either --doi or --ris must be provided.")

    inst = auth_setup.get_active_institution()

    if args.ris:
        records = parse_ris_file(args.ris)
        if not records:
            print(f"No records found in RIS file: {args.ris}", file=sys.stderr)
            return 1
        print(f"Found {len(records)} citation record(s) in {args.ris}.")
        failed_count = 0
        for rec in records:
            try:
                process_target(
                    ris_record=rec,
                    institution=inst,
                    state_path=args.state_path,
                    output_dir=args.output_dir,
                    source_text_dir=args.source_text_dir,
                    zotero_dir=args.zotero_dir,
                    zotero_collection=args.zotero_collection,
                    skip_download=args.skip_download,
                    skip_markdown=args.skip_markdown,
                    skip_zotero=args.skip_zotero,
                    skip_oa=args.skip_oa,
                    headless=not args.no_headless,
                    interactive_fallback=False,
                    is_batch=True
                )
            except Exception as e:
                failed_count += 1
                rec_doi = rec.get("doi") or "unknown"
                print(f"Batch processing error for {rec_doi}: {e}", file=sys.stderr)

        if failed_count > 0:
            print(f"\nBatch processing completed with {failed_count} failure(s). Check working/failed_papers.log and working/error_audit.jsonl.")
            return 1
        return 0
    else:
        try:
            process_target(
                doi=args.doi,
                institution=inst,
                state_path=args.state_path,
                output_dir=args.output_dir,
                source_text_dir=args.source_text_dir,
                zotero_dir=args.zotero_dir,
                zotero_collection=args.zotero_collection,
                skip_download=args.skip_download,
                skip_markdown=args.skip_markdown,
                skip_zotero=args.skip_zotero,
                skip_oa=args.skip_oa,
                headless=not args.no_headless,
                interactive_fallback=args.interactive_fallback,
                is_batch=False
            )
        except Exception as e:
            print(f"Processing error: {e}", file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
