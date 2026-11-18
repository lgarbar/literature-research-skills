#!/usr/bin/env python3
"""Unit tests for resilient 2FA/SSO challenge detection and interactive fallback."""
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import auth_setup
import fetch_and_process


class TestAuthFallback(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.state_file = Path(self.temp_dir) / "storage_state.json"
        self.output_dir = Path(self.temp_dir) / "downloads"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Write dummy session state
        self.state_file.write_text(json.dumps({
            "cookies": [{"name": "ezproxy", "value": "dummy_cookie", "domain": "proxy.library.vanderbilt.edu"}],
            "origins": []
        }), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_is_login_or_challenge_url_detection(self):
        # Known challenge hosts and endpoints
        self.assertTrue(auth_setup.is_login_or_challenge(url="https://api-1234.duosecurity.com/frame/prompt?sid=123"))
        self.assertTrue(auth_setup.is_login_or_challenge(url="https://onevu.vanderbilt.edu/idp/profile/SAML2/Redirect/SSO"))
        self.assertTrue(auth_setup.is_login_or_challenge(url="https://vanderbilt.okta.com/login/login.htm"))
        self.assertTrue(auth_setup.is_login_or_challenge(url="http://proxy.library.vanderbilt.edu/login?url=https://doi.org/..."))
        self.assertTrue(auth_setup.is_login_or_challenge(url="https://login.microsoftonline.com/common/oauth2/authorize"))

        # Non-challenge article endpoints
        self.assertFalse(auth_setup.is_login_or_challenge(url="https://www.sciencedirect.com/science/article/pii/S016622360600248X"))
        self.assertFalse(auth_setup.is_login_or_challenge(url="https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/cphy.c170043"))

    def test_is_login_or_challenge_dom_detection(self):
        # Mock Playwright page with Duo prompt
        mock_page = MagicMock()
        mock_page.url = "https://proxy.library.vanderbilt.edu/target"
        mock_page.title.return_value = "Institutional Login"
        mock_page.inner_text.return_value = "Check for a Duo Push to verify your identity"

        def mock_locator(sel):
            loc = MagicMock()
            if "duo" in sel:
                loc.count.return_value = 1
            else:
                loc.count.return_value = 0
            return loc

        mock_page.locator.side_effect = mock_locator
        self.assertTrue(auth_setup.is_login_or_challenge(page=mock_page))

        # Mock page on normal article
        mock_article_page = MagicMock()
        mock_article_page.url = "https://www-sciencedirect-com.proxy.library.vanderbilt.edu/science/article/pii/12345"
        mock_article_page.title.return_value = "Journal of Neuroscience"
        mock_article_page.locator.return_value.count.return_value = 0
        mock_article_page.inner_text.return_value = "Introduction: We studied single-neuron recordings in behaving primates..." * 100
        self.assertFalse(auth_setup.is_login_or_challenge(page=mock_article_page))

    @patch("auth_setup.authenticate_interactive")
    def test_validate_session_triggers_headed_fallback_on_challenge(self, mock_auth):
        mock_auth.return_value = True

        # Simulate headless session check encountering an SSO login challenge
        with patch("auth_setup.get_existing_state_path", return_value=self.state_file):
            with patch("auth_setup.is_login_or_challenge", return_value=True):
                # Mock sync_playwright
                mock_p = MagicMock()
                mock_browser = MagicMock()
                mock_context = MagicMock()
                mock_page = MagicMock()
                mock_context.new_page.return_value = mock_page
                mock_browser.new_context.return_value = mock_context
                mock_p.chromium.launch.return_value = mock_browser

                with patch("playwright.sync_api.sync_playwright") as mock_sp:
                    mock_sp.return_value.__enter__.return_value = mock_p

                    captured_out = io.StringIO()
                    with patch("sys.stdout", captured_out):
                        valid = auth_setup.validate_session(
                            state_path=self.state_file,
                            interactive_fallback=True
                        )

                    self.assertTrue(valid)
                    # Verify terminal prompt message
                    self.assertIn("Institutional session expired or 2FA required. Please complete authentication in the browser window...", captured_out.getvalue())
                    # Verify authenticate_interactive was called with headed mode (headless=False)
                    mock_auth.assert_called_once()
                    self.assertFalse(mock_auth.call_args[1]["headless"])

    def test_step_a_handles_challenge_and_resumes_pipeline(self):
        doi = "10.1016/j.tics.2006.10.012"
        inst = {
            "id": "vanderbilt",
            "name": "Vanderbilt University",
            "ezproxy_prefix": "http://proxy.library.vanderbilt.edu/login?url=",
            "login_portal": "http://proxy.library.vanderbilt.edu/login"
        }

        # Simulate first attempt hitting a Duo 2FA challenge, followed by headed authentication,
        # and second attempt succeeding with a valid PDF download.
        call_count = 0

        def fake_authenticate_interactive(*args, **kwargs):
            # Verify headed mode is requested
            self.assertFalse(kwargs.get("headless", True))
            # Write updated state file
            self.state_file.write_text(json.dumps({
                "cookies": [{"name": "ezproxy", "value": "fresh_authenticated_cookie", "domain": "proxy.library.vanderbilt.edu"}],
                "origins": []
            }), encoding="utf-8")
            return True

        with patch("fetch_and_process.query_crossref_oa_links", return_value=[]):
            with patch("fetch_and_process.query_unpaywall_oa_links", return_value=[]):
                with patch("auth_setup.get_existing_state_path", return_value=self.state_file):
                    with patch("auth_setup.authenticate_interactive", side_effect=fake_authenticate_interactive) as mock_auth:
                        with patch("playwright.sync_api.sync_playwright") as mock_sp:
                            mock_p = MagicMock()
                            mock_browser = MagicMock()
                            mock_context = MagicMock()
                            mock_page = MagicMock()

                            mock_context.new_page.return_value = mock_page
                            mock_browser.new_context.return_value = mock_context
                            mock_p.chromium.launch.return_value = mock_browser
                            mock_sp.return_value.__enter__.return_value = mock_p

                            def fake_goto(url, *args, **kwargs):
                                nonlocal call_count
                                call_count += 1
                                if call_count == 1:
                                    # First attempt: hit institutional 2FA/SSO challenge
                                    mock_page.url = "https://onevu.vanderbilt.edu/idp/profile/SAML2/Redirect/SSO"
                                else:
                                    # Second attempt: successful navigation to article
                                    mock_page.url = "https://www-sciencedirect-com.proxy.library.vanderbilt.edu/science/article/pii/S016622360600248X"

                            mock_page.goto.side_effect = fake_goto

                            # Set up PDF download on attempt 2
                            def fake_request_get(url, *args, **kwargs):
                                resp = MagicMock()
                                resp.url = url
                                resp.status = 200
                                resp.headers = {"content-type": "application/pdf"}
                                resp.body.return_value = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n" + b"X" * 15000
                                return resp

                            mock_context.request.get.side_effect = fake_request_get

                            with patch("fetch_and_process.source_files.inspect_file") as mock_inspect:
                                mock_inspect.return_value = ({"mechanical_status": "pass", "bytes": 15050, "page_count": 8}, "")

                                captured_out = io.StringIO()
                                with patch("sys.stdout", captured_out):
                                    pdf_path, rep = fetch_and_process.step_a_download_paper(
                                        doi=doi,
                                        institution=inst,
                                        state_path=self.state_file,
                                        output_dir=self.output_dir,
                                        headless=True,
                                        skip_oa=True,
                                        interactive_fallback=True
                                    )

                                self.assertTrue(pdf_path.is_file())
                                self.assertEqual(rep["mechanical_status"], "pass")
                                # Verify prompt was output
                                self.assertIn("Institutional session expired or 2FA required. Please complete authentication in the browser window...", captured_out.getvalue())
                                # Verify fallback was executed
                                mock_auth.assert_called_once()
                                self.assertEqual(call_count, 2)


if __name__ == "__main__":
    unittest.main()
