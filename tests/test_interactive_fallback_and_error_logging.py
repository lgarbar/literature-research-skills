#!/usr/bin/env python3
"""Unit tests for Interactive Headed Fallback and Robust Two-Tier Error Logging."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import auth_setup
import fetch_and_process
from fetch_and_process import (
    ANTI_BOT_PROMPT,
    InvalidPDFError,
    log_failure,
    interactive_headed_download,
    step_a_download_paper,
    process_target,
    MIN_PDF_SIZE_BYTES,
)


class TestInteractiveHeadedFallbackAndErrorLogging(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.downloads_dir = self.temp_path / "downloads"
        self.working_dir = self.temp_path / "working"
        self.source_text_dir = self.working_dir / "source-text"
        self.zotero_dir = self.working_dir / "zotero_intake"

        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self.source_text_dir.mkdir(parents=True, exist_ok=True)
        self.zotero_dir.mkdir(parents=True, exist_ok=True)

        self.state_file = self.temp_path / "storage_state.json"
        self.state_file.write_text("{}", encoding="utf-8")

        self.valid_pdf_payload = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n" + b"X" * 150000

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_anti_bot_prompt_string_exact_match(self):
        expected_prompt = (
            "Automated retrieval blocked by anti-bot challenge. "
            "Please complete the challenge or download the PDF manually in the browser, then press Enter to resume..."
        )
        self.assertEqual(ANTI_BOT_PROMPT, expected_prompt)

    def test_two_tier_error_logging_audit_jsonl_and_deduplication(self):
        doi = "10.1016/j.cell.2024.01.001"
        stage = "step_a_download"
        error = InvalidPDFError("Publisher returned an HTML fallback challenge page.")
        endpoint_url = "https://www.sciencedirect.com/science/article/pii/S12345/pdfft?isDTMRedir=true&download=true"

        audit_file = self.working_dir / "error_audit.jsonl"
        summary_file = self.working_dir / "failed_papers.log"

        # Log first failure
        log_failure(
            doi=doi,
            failure_stage=stage,
            error=error,
            endpoint_url=endpoint_url,
            working_dir=self.working_dir
        )

        self.assertTrue(audit_file.is_file(), "error_audit.jsonl must exist")
        self.assertTrue(summary_file.is_file(), "failed_papers.log must exist")

        audit_lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(audit_lines), 1)
        entry = json.loads(audit_lines[0])
        self.assertEqual(entry["doi"], doi)
        self.assertEqual(entry["failure_stage"], stage)
        self.assertEqual(entry["error_type"], "InvalidPDFError")
        self.assertEqual(entry["endpoint_url"], endpoint_url)
        self.assertIn("HTML fallback", entry["error_message"])
        self.assertIn("timestamp", entry)

        summary_lines = summary_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(summary_lines), 1)
        self.assertIn(doi, summary_lines[0])
        self.assertIn(stage, summary_lines[0])

        # Log failure again with the same DOI (simulating repeat attempt / run)
        log_failure(
            doi=doi,
            failure_stage=stage,
            error=RuntimeError("Subsequent failure on retry"),
            endpoint_url=endpoint_url,
            working_dir=self.working_dir
        )

        # Tier 1 audit log captures both events
        audit_lines_2 = audit_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(audit_lines_2), 2, "error_audit.jsonl should append all failure events")

        # Tier 2 summary log must be deduplicated
        summary_lines_2 = summary_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(summary_lines_2), 1, "failed_papers.log must deduplicate entries by DOI")

    @patch("fetch_and_process.source_files.inspect_file")
    def test_interactive_headed_download_launches_headed_and_prompts_user(self, mock_inspect):
        mock_inspect.return_value = ({"mechanical_status": "pass", "bytes": 150000, "page_count": 8}, "")
        doi = "10.1016/j.cell.2024.01.002"
        target_url = "https://www.sciencedirect.com/science/article/pii/S54321"
        pdf_dest = self.downloads_dir / "10.1016_j.cell.2024.01.002.pdf"

        prompt_captured = None

        def fake_input(prompt_text):
            nonlocal prompt_captured
            prompt_captured = prompt_text
            # Simulate the user completing the challenge / manual download in the visible window
            pdf_dest.write_bytes(self.valid_pdf_payload)
            return ""

        with patch("builtins.input", side_effect=fake_input):
            with patch("playwright.sync_api.sync_playwright") as mock_sp:
                mock_p = MagicMock()
                mock_browser = MagicMock()
                mock_context = MagicMock()
                mock_page = MagicMock()

                mock_context.new_page.return_value = mock_page
                mock_browser.new_context.return_value = mock_context
                mock_p.chromium.launch.return_value = mock_browser
                mock_sp.return_value.__enter__.return_value = mock_p

                res_pdf, rep = interactive_headed_download(
                    doi=doi,
                    target_url=target_url,
                    pdf_path=pdf_dest,
                    session_file=self.state_file
                )

                # Verify browser was launched with headless=False
                mock_p.chromium.launch.assert_called_once()
                self.assertFalse(mock_p.chromium.launch.call_args[1].get("headless"))

                # Verify the prompt text presented to the user
                self.assertIsNotNone(prompt_captured)
                self.assertIn(ANTI_BOT_PROMPT, prompt_captured)

                # Verify PDF was verified and returned
                self.assertEqual(res_pdf.resolve(), pdf_dest.resolve())
                self.assertEqual(rep["mechanical_status"], "pass")
                self.assertTrue(pdf_dest.is_file())

    @patch("fetch_and_process.source_files.inspect_file")
    def test_interactive_fallback_in_single_doi_mode_end_to_end(self, mock_inspect):
        mock_inspect.return_value = ({"mechanical_status": "pass", "bytes": 150000, "page_count": 12}, "")
        doi = "10.1016/j.cell.2024.01.003"
        safe_name = "10.1016_j.cell.2024.01.003"
        pdf_path = self.downloads_dir / f"{safe_name}.pdf"

        # Headless attempt fails with HTML fallback; interactive headed session succeeds
        headed_launched = False

        def fake_launch(*args, **kwargs):
            nonlocal headed_launched
            mock_browser = MagicMock()
            is_headless = kwargs.get("headless", True)
            if not is_headless:
                headed_launched = True
            return mock_browser

        with patch("auth_setup.get_existing_state_path", return_value=self.state_file):
            with patch("auth_setup.is_login_or_challenge", return_value=False):
                with patch("playwright.sync_api.sync_playwright") as mock_sp:
                    mock_p = MagicMock()
                    mock_p.chromium.launch.side_effect = fake_launch
                    mock_context = MagicMock()
                    mock_page = MagicMock()
                    mock_context.new_page.return_value = mock_page
                    mock_sp.return_value.__enter__.return_value = mock_p

                    # Headless navigation returns HTML fallback
                    html_fallback = b"<!DOCTYPE html><html><body><h1>Anti-bot challenge</h1></body></html>"
                    mock_dl = MagicMock()
                    mock_dl.save_as.side_effect = lambda dest: Path(dest).write_bytes(html_fallback)
                    mock_page.expect_download.return_value.__enter__.return_value = MagicMock(value=mock_dl)
                    mock_page.content.return_value = html_fallback.decode("utf-8")
                    mock_page.url = "https://www.sciencedirect.com/science/article/pii/S11111"

                    # When prompt is shown in headed mode, simulate valid PDF saved
                    def fake_input(prompt_text):
                        pdf_path.write_bytes(self.valid_pdf_payload)
                        return ""

                    with patch("builtins.input", side_effect=fake_input):
                        with patch("fetch_and_process.pymupdf4llm.to_markdown", return_value="# Cell Article\n\nFull text of article."):
                            result = process_target(
                                doi=doi,
                                institution={"id": "vanderbilt", "name": "Vanderbilt University", "ezproxy_prefix": "https://proxy..."},
                                state_path=self.state_file,
                                output_dir=self.downloads_dir,
                                source_text_dir=self.source_text_dir,
                                zotero_dir=self.zotero_dir,
                                skip_oa=True,
                                headless=True,
                                interactive_fallback=True,
                                is_batch=False
                            )

                            self.assertTrue(headed_launched, "Must switch to headed mode (headless=False) on anti-bot/HTML fallback")
                            self.assertEqual(result["doi"], doi)
                            self.assertTrue(Path(result["pdf_path"]).is_file())
                            self.assertTrue(Path(result["markdown_path"]).is_file())
                            self.assertIsNotNone(result["zotero"])

    def test_safeguard_disables_headed_fallback_in_batch_mode(self):
        doi = "10.1016/j.cell.2024.01.004"
        out_dir = self.downloads_dir

        with patch("auth_setup.get_existing_state_path", return_value=self.state_file):
            with patch("auth_setup.is_login_or_challenge", return_value=False):
                with patch("playwright.sync_api.sync_playwright") as mock_sp:
                    mock_p = MagicMock()
                    mock_browser = MagicMock()
                    mock_context = MagicMock()
                    mock_page = MagicMock()
                    mock_context.new_page.return_value = mock_page
                    mock_browser.new_context.return_value = mock_context
                    mock_p.chromium.launch.return_value = mock_browser
                    mock_sp.return_value.__enter__.return_value = mock_p

                    # Headless navigation returns HTML fallback
                    html_fallback = b"<!DOCTYPE html><html><body><h1>Cloudflare challenge</h1></body></html>"
                    mock_dl = MagicMock()
                    mock_dl.save_as.side_effect = lambda dest: Path(dest).write_bytes(html_fallback)
                    mock_page.expect_download.return_value.__enter__.return_value = MagicMock(value=mock_dl)
                    mock_page.content.return_value = html_fallback.decode("utf-8")
                    mock_page.url = "https://www.sciencedirect.com/science/article/pii/S22222"

                    with patch("fetch_and_process.interactive_headed_download") as mock_headed:
                        with self.assertRaises(InvalidPDFError):
                            step_a_download_paper(
                                doi=doi,
                                institution={"id": "vanderbilt", "name": "Vanderbilt University", "ezproxy_prefix": "https://proxy..."},
                                state_path=self.state_file,
                                output_dir=out_dir,
                                skip_oa=True,
                                headless=True,
                                interactive_fallback=True,
                                is_batch=True  # Batch mode safeguard active!
                            )

                        # Must never invoke interactive headed download when is_batch=True
                        mock_headed.assert_not_called()

                        # Must route failure straight to error logging
                        audit_file = ROOT / "working" / "error_audit.jsonl"
                        summary_file = ROOT / "working" / "failed_papers.log"
                        self.assertTrue(audit_file.is_file() or (out_dir.parent / "working" / "error_audit.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
