#!/usr/bin/env python3
"""Unit tests for Automated Print-to-PDF Fallback, Title/Flag Marking, Manual Queue Logging, and Batch Mode."""
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
from fetch_and_process import (
    PRINT_CAPTURE_TAG,
    MANUAL_PDF_URLS_JSON,
    MANUAL_PDF_URLS_TXT,
    InvalidPDFError,
    validate_pdf_stream,
    is_valid_pdf_stream,
    log_manual_pdf_url,
    render_print_to_pdf_fallback,
    step_a_download_paper,
    process_target,
)


class TestPrintToPDFFallback(unittest.TestCase):
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
        self.state_file.write_text(json.dumps({
            "cookies": [
                {"name": "ezproxy", "value": "test_token_123", "domain": "proxy.library.vanderbilt.edu"},
            ],
            "origins": []
        }), encoding="utf-8")

        # Synthetic valid PDF payload (> 1 KB)
        self.valid_print_pdf_bytes = b"%PDF-1.5\n1 0 obj\n<< /Type /Catalog >>\nendobj\n" + (b"Article print body text line.\n" * 50) + b"%%EOF\n"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_validate_pdf_stream_with_print_fallback(self):
        # Strict validation rejects PDF <= 100 KB
        self.assertFalse(is_valid_pdf_stream(self.valid_print_pdf_bytes, allow_print_fallback=False))
        with self.assertRaises(InvalidPDFError):
            validate_pdf_stream(self.valid_print_pdf_bytes, allow_print_fallback=False)

        # allow_print_fallback=True accepts valid PDF > 500 bytes
        self.assertTrue(validate_pdf_stream(self.valid_print_pdf_bytes, allow_print_fallback=True))
        self.assertTrue(is_valid_pdf_stream(self.valid_print_pdf_bytes, allow_print_fallback=True))

        # Still rejects raw HTML even when allow_print_fallback=True
        raw_html = b"<!DOCTYPE html><html><body>Error 600010</body></html>"
        with self.assertRaises(InvalidPDFError):
            validate_pdf_stream(raw_html, allow_print_fallback=True)

    def test_render_print_to_pdf_fallback_success(self):
        pdf_dest = self.downloads_dir / "10.1016_j.cell.2024.01.001.pdf"
        target_url = "https://www.sciencedirect.com/science/article/pii/S12345"

        mock_page = MagicMock()
        mock_page.url = "https://www.sciencedirect.com/science/article/pii/S12345/pdfft?download=true"
        mock_page.pdf.return_value = self.valid_print_pdf_bytes

        with patch("fetch_and_process.source_files.inspect_file") as mock_inspect:
            mock_inspect.return_value = ({"mechanical_status": "pass", "bytes": len(self.valid_print_pdf_bytes), "page_count": 3}, "")
            res_pdf, rep = render_print_to_pdf_fallback(
                page=mock_page,
                target_article_url=target_url,
                pdf_path=pdf_dest
            )

            self.assertIsNotNone(res_pdf)
            self.assertTrue(pdf_dest.is_file())
            self.assertEqual(res_pdf.resolve(), pdf_dest.resolve())
            self.assertTrue(rep.get("is_print_fallback"))
            self.assertTrue(rep.get("print_capture"))
            self.assertEqual(rep.get("print_capture_tag"), PRINT_CAPTURE_TAG)
            # Verify page navigated to article page before pdf export
            mock_page.goto.assert_called_with(target_url, wait_until="domcontentloaded", timeout=45000)
            mock_page.pdf.assert_called_once_with(print_background=True)

    def test_manual_pdf_urls_logging_and_deduplication(self):
        doi_1 = "10.1016/j.cell.2024.01.001"
        title_1 = "Groundbreaking Biological Discovery"
        url_1 = "https://www.sciencedirect.com/science/article/pii/S12345"

        json_file = self.working_dir / MANUAL_PDF_URLS_JSON
        txt_file = self.working_dir / MANUAL_PDF_URLS_TXT

        # 1. First record
        log_manual_pdf_url(
            doi=doi_1,
            title=f"{title_1} {PRINT_CAPTURE_TAG}",
            target_article_url=url_1,
            working_dir=self.working_dir
        )

        self.assertTrue(json_file.is_file())
        self.assertTrue(txt_file.is_file())

        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["doi"], doi_1)
        self.assertEqual(data[0]["title"], title_1)
        self.assertEqual(data[0]["target_article_url"], url_1)
        self.assertIn("timestamp", data[0])

        txt_lines = txt_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(txt_lines), 1)
        self.assertIn(doi_1, txt_lines[0])
        self.assertIn(title_1, txt_lines[0])
        self.assertIn(url_1, txt_lines[0])

        # 2. Duplicate record attempt (repeat run)
        log_manual_pdf_url(
            doi=doi_1,
            title=title_1,
            target_article_url=url_1,
            working_dir=self.working_dir
        )

        with open(json_file, "r", encoding="utf-8") as f:
            data_after = json.load(f)
        self.assertEqual(len(data_after), 1, "manual_pdf_urls.json must deduplicate by DOI")

        txt_lines_after = txt_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(txt_lines_after), 1, "manual_pdf_urls.txt must deduplicate by DOI")

        # 3. Distinct second record
        doi_2 = "10.1016/j.neuron.2024.02.002"
        title_2 = "Novel Neural Circuit Dynamics"
        url_2 = "https://www.sciencedirect.com/science/article/pii/S67890"

        log_manual_pdf_url(
            doi=doi_2,
            title=title_2,
            target_article_url=url_2,
            working_dir=self.working_dir
        )

        with open(json_file, "r", encoding="utf-8") as f:
            data_2 = json.load(f)
        self.assertEqual(len(data_2), 2)
        self.assertEqual(data_2[1]["doi"], doi_2)

        txt_lines_2 = txt_file.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(txt_lines_2), 2)
        self.assertIn(doi_2, txt_lines_2[1])

    @patch("fetch_and_process.source_files.inspect_file")
    def test_process_target_automated_print_to_pdf_end_to_end(self, mock_inspect):
        mock_inspect.return_value = ({"mechanical_status": "pass", "bytes": len(self.valid_print_pdf_bytes), "page_count": 5}, "")
        doi = "10.1016/j.cell.2024.01.020"
        title = "Neural Correlates of Memory Consolidation"

        with patch("auth_setup.get_existing_state_path", return_value=self.state_file):
            with patch("auth_setup.is_login_or_challenge", return_value=False):
                with patch("playwright.sync_api.sync_playwright") as mock_sp:
                    mock_p = MagicMock()
                    mock_browser = MagicMock()
                    mock_context = MagicMock()
                    mock_page = MagicMock()
                    mock_context.pages = [mock_page]
                    mock_context.new_page.return_value = mock_page
                    mock_browser.new_context.return_value = mock_context
                    mock_p.chromium.launch.return_value = mock_browser
                    mock_sp.return_value.__enter__.return_value = mock_p

                    # ScienceDirect download endpoint returns HTML error (e.g. 600010)
                    html_fallback = b"<!DOCTYPE html><html><body><h1>Error (600010)</h1></body></html>"
                    mock_dl = MagicMock()
                    mock_dl.save_as.side_effect = lambda dest: Path(dest).write_bytes(html_fallback)
                    mock_page.expect_download.return_value.__enter__.return_value = MagicMock(value=mock_dl)
                    mock_page.content.return_value = html_fallback.decode("utf-8")
                    mock_page.url = "https://www.sciencedirect.com/science/article/pii/S11111/pdfft"

                    # page.pdf succeeds
                    mock_page.pdf.return_value = self.valid_print_pdf_bytes

                    with patch("fetch_and_process.pymupdf4llm.to_markdown", return_value="# Article Title\n\nFull article content parsed."):
                        with patch("fetch_and_process.resolve_doi_metadata", return_value={"title": title, "year": "2024", "venue": "Cell", "authors": ["Researcher, Alice"], "url": "https://doi.org/10.1016/j.cell.2024.01.020"}):
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
                                is_batch=False,
                                working_dir=self.working_dir
                            )

                            self.assertEqual(result["doi"], doi)
                            self.assertTrue(result["is_print_fallback"])
                            self.assertTrue(Path(result["pdf_path"]).is_file())
                            self.assertTrue(Path(result["markdown_path"]).is_file())

                            # Check markdown YAML frontmatter tagging
                            md_content = Path(result["markdown_path"]).read_text(encoding="utf-8")
                            self.assertIn(PRINT_CAPTURE_TAG, md_content)
                            self.assertIn("is_print_fallback: true", md_content)
                            self.assertIn("print_capture: true", md_content)

                            # Check Zotero package manifest tagging
                            pkg_dir = Path(result["zotero"]["package_dir"])
                            manifest_path = pkg_dir / "manifest.json"
                            self.assertTrue(manifest_path.is_file())
                            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
                            self.assertIn(PRINT_CAPTURE_TAG, manifest_data["title"])
                            self.assertTrue(manifest_data["is_print_fallback"])
                            self.assertIn("print-capture-fallback", manifest_data["tags"])
                            self.assertIn("not-publisher-formatted-pdf", manifest_data["tags"])

                            # Check RIS citation tagging
                            ris_path = pkg_dir / "citation.ris"
                            ris_content = ris_path.read_text(encoding="utf-8")
                            self.assertIn(PRINT_CAPTURE_TAG, ris_content)

                            # Check manual download queue logging
                            json_queue = self.working_dir / MANUAL_PDF_URLS_JSON
                            self.assertTrue(json_queue.is_file())
                            with open(json_queue, "r", encoding="utf-8") as f:
                                queue_data = json.load(f)
                            self.assertEqual(len(queue_data), 1)
                            self.assertEqual(queue_data[0]["doi"], doi)

    @patch("fetch_and_process.source_files.inspect_file")
    def test_batch_mode_print_to_pdf_fallback_without_interactive_prompts(self, mock_inspect):
        mock_inspect.return_value = ({"mechanical_status": "pass", "bytes": len(self.valid_print_pdf_bytes), "page_count": 6}, "")
        doi = "10.1016/j.cell.2024.01.030"

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

                    # Simulate Cloudflare or ScienceDirect error 600010
                    html_fallback = b"<!DOCTYPE html><html><body><h1>Anti-bot challenge</h1></body></html>"
                    mock_dl = MagicMock()
                    mock_dl.save_as.side_effect = lambda dest: Path(dest).write_bytes(html_fallback)
                    mock_page.expect_download.return_value.__enter__.return_value = MagicMock(value=mock_dl)
                    mock_page.content.return_value = html_fallback.decode("utf-8")
                    mock_page.url = "https://www.sciencedirect.com/science/article/pii/S33333/pdfft"

                    # page.pdf succeeds automatically
                    mock_page.pdf.return_value = self.valid_print_pdf_bytes

                    with patch("fetch_and_process.interactive_headed_download") as mock_headed:
                        with patch("builtins.input") as mock_input:
                            pdf_path, rep = step_a_download_paper(
                                doi=doi,
                                institution={"id": "vanderbilt", "name": "Vanderbilt University", "ezproxy_prefix": "https://proxy..."},
                                state_path=self.state_file,
                                output_dir=self.downloads_dir,
                                skip_oa=True,
                                headless=True,
                                interactive_fallback=True,
                                is_batch=True  # Batch mode!
                            )

                            self.assertTrue(pdf_path.is_file())
                            self.assertTrue(rep["is_print_fallback"])
                            # In batch mode, must NOT block on user input or launch headed download
                            mock_headed.assert_not_called()
                            mock_input.assert_not_called()

    def test_batch_mode_fails_and_logs_when_print_fallback_also_fails(self):
        doi = "10.1016/j.cell.2024.01.040"

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

                    # Both direct download and page.pdf fail
                    html_fallback = b"<!DOCTYPE html><html><body><h1>Anti-bot challenge</h1></body></html>"
                    mock_dl = MagicMock()
                    mock_dl.save_as.side_effect = lambda dest: Path(dest).write_bytes(html_fallback)
                    mock_page.expect_download.return_value.__enter__.return_value = MagicMock(value=mock_dl)
                    mock_page.content.return_value = html_fallback.decode("utf-8")
                    mock_page.url = "https://www.sciencedirect.com/science/article/pii/S44444"
                    mock_page.pdf.side_effect = RuntimeError("PDF generation failed")

                    with patch("fetch_and_process.interactive_headed_download") as mock_headed:
                        with patch("builtins.input") as mock_input:
                            with self.assertRaises(InvalidPDFError):
                                step_a_download_paper(
                                    doi=doi,
                                    institution={"id": "vanderbilt", "name": "Vanderbilt University", "ezproxy_prefix": "https://proxy..."},
                                    state_path=self.state_file,
                                    output_dir=self.downloads_dir,
                                    skip_oa=True,
                                    headless=True,
                                    interactive_fallback=True,
                                    is_batch=True
                                )

                            mock_headed.assert_not_called()
                            mock_input.assert_not_called()

                            audit_file = ROOT / "working" / "error_audit.jsonl"
                            summary_file = ROOT / "working" / "failed_papers.log"
                            self.assertTrue(audit_file.is_file())
                            self.assertTrue(summary_file.is_file())


if __name__ == "__main__":
    unittest.main()
