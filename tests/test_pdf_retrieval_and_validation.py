#!/usr/bin/env python3
"""Unit tests for robust PDF retrieval, DOM indexing, and quality validation."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_and_process
from fetch_and_process import (
    InvalidPDFError,
    validate_pdf_stream,
    is_valid_pdf_stream,
    SCIENCEDIRECT_PDF_SELECTORS,
    MAJOR_PUBLISHER_PDF_SELECTORS,
    MIN_PDF_SIZE_BYTES,
)


class TestPDFValidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_valid_pdf_stream_passes(self):
        valid_bytes = b"%PDF-1.7\n" + b"X" * (120 * 1024)
        self.assertTrue(validate_pdf_stream(valid_bytes))
        self.assertTrue(is_valid_pdf_stream(valid_bytes))

        pdf_file = self.temp_path / "valid.pdf"
        pdf_file.write_bytes(valid_bytes)
        self.assertTrue(validate_pdf_stream(pdf_file))
        self.assertTrue(is_valid_pdf_stream(pdf_file))

    def test_missing_magic_bytes_rejected(self):
        # Plain text
        text_bytes = b"This is a text file, not a PDF." * 5000
        with self.assertRaises(InvalidPDFError) as ctx:
            validate_pdf_stream(text_bytes)
        self.assertIn("missing %PDF- magic bytes header", str(ctx.exception))

    def test_html_document_fallback_rejected(self):
        # HTML error/print fallback
        html_bytes = b"<!DOCTYPE html><html><head><title>Print View</title></head><body>" + b"<p>Article content</p>" * 8000 + b"</body></html>"
        with self.assertRaises(InvalidPDFError) as ctx:
            validate_pdf_stream(html_bytes)
        self.assertIn("HTML document", str(ctx.exception))
        self.assertIn("binary PDF stream", str(ctx.exception))

    def test_pdf_below_100kb_threshold_rejected(self):
        # PDF header present but size is below 100 KB (e.g. print stub, 15 KB or 50 KB)
        stub_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n" + b"A" * (25 * 1024)
        self.assertFalse(is_valid_pdf_stream(stub_bytes))
        with self.assertRaises(InvalidPDFError) as ctx:
            validate_pdf_stream(stub_bytes)
        self.assertIn("minimum threshold", str(ctx.exception))
        self.assertIn("100 KB", str(ctx.exception))

    def test_step_c_rejects_invalid_pdf_and_does_not_save_paper_pdf(self):
        # Verify that an invalid stub is not saved as paper.pdf in the intake package
        invalid_pdf = self.temp_path / "stub.pdf"
        invalid_pdf.write_bytes(b"%PDF-1.4\n" + b"A" * 5000)  # Only ~5 KB
        notes_md = self.temp_path / "notes.md"
        notes_md.write_text("# Notes", encoding="utf-8")
        zotero_out = self.temp_path / "zotero_pkg"

        with self.assertRaises(InvalidPDFError):
            fetch_and_process.step_c_sync_or_package_zotero(
                pdf_path=invalid_pdf,
                markdown_path=notes_md,
                metadata={"title": "Test Paper", "doi": "10.1016/j.test"},
                zotero_dir=zotero_out
            )

        saved_paper_pdf = zotero_out / "10.1016_j.test" / "paper.pdf"
        self.assertFalse(saved_paper_pdf.exists())

    def test_step_b_rejects_invalid_pdf_before_markdown_conversion(self):
        html_fallback = self.temp_path / "fallback.pdf"
        html_fallback.write_bytes(b"<html><body><h1>Fallback page</h1></body></html>" + b"X" * 150000)

        with self.assertRaises(InvalidPDFError):
            fetch_and_process.step_b_convert_to_markdown(
                pdf_path=html_fallback,
                metadata={"title": "Test Paper", "doi": "10.1016/j.test"},
                source_text_dir=self.temp_path / "source-text"
            )


class TestScienceDirectDOMIndexing(unittest.TestCase):
    def test_sciencedirect_selectors_presence(self):
        # Requirement 1: Checking selectors like #pdf-download-link, a.pdf-download-link,
        # [data-aa-name="pdf-download"], div.pdf-link, or anchor tags containing /pdfft? or download=true
        expected_tokens = [
            "#pdf-download-link",
            "a.pdf-download-link",
            "[data-aa-name='pdf-download']",
            "div.pdf-link",
            "/pdfft?",
            "download=true",
        ]
        for tok in expected_tokens:
            self.assertTrue(
                any(tok in s for s in SCIENCEDIRECT_PDF_SELECTORS),
                f"Expected selector containing '{tok}' in SCIENCEDIRECT_PDF_SELECTORS"
            )

    def test_major_publisher_selectors_presence(self):
        for tok in ["a.pdf-download-btn", "pdfdirect", "/doi/pdf/", "Download PDF"]:
            self.assertTrue(
                any(tok in s for s in MAJOR_PUBLISHER_PDF_SELECTORS),
                f"Expected selector containing '{tok}' in MAJOR_PUBLISHER_PDF_SELECTORS"
            )

    @patch("fetch_and_process.source_files.inspect_file")
    def test_sciencedirect_download_via_expect_download(self, mock_inspect):
        mock_inspect.return_value = ({"mechanical_status": "pass", "bytes": 150000, "page_count": 10}, "")
        temp_dir = tempfile.mkdtemp()
        state_file = Path(temp_dir) / "storage_state.json"
        state_file.write_text("{}", encoding="utf-8")
        out_dir = Path(temp_dir) / "downloads"
        out_dir.mkdir(parents=True, exist_ok=True)

        valid_pdf_payload = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n" + b"Z" * 150000

        with patch("auth_setup.get_existing_state_path", return_value=state_file):
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

                    mock_page.url = "https://www-sciencedirect-com.proxy.library.vanderbilt.edu/science/article/pii/S12345"
                    mock_page.evaluate.return_value = "text/html"

                    # Simulate finding #pdf-download-link button
                    mock_btn = MagicMock()
                    mock_btn.get_attribute.return_value = None  # no direct href

                    def query_selector_mock(sel):
                        if sel == "#pdf-download-link":
                            return mock_btn
                        return None

                    mock_page.query_selector.side_effect = query_selector_mock

                    # Simulate Playwright expect_download capturing the file
                    mock_download = MagicMock()
                    def fake_save_as(dest):
                        Path(dest).write_bytes(valid_pdf_payload)

                    mock_download.save_as.side_effect = fake_save_as
                    mock_page.expect_download.return_value.__enter__.return_value = MagicMock(value=mock_download)

                    pdf_path, rep = fetch_and_process.step_a_download_paper(
                        doi="10.1016/j.cell.2023.01.001",
                        institution={"id": "vanderbilt", "name": "Vanderbilt University", "ezproxy_prefix": "https://proxy..."},
                        state_path=state_file,
                        output_dir=out_dir,
                        headless=True,
                        skip_oa=True
                    )

                    self.assertTrue(pdf_path.is_file())
                    self.assertEqual(rep["mechanical_status"], "pass")
                    # Ensure btn.click() was triggered within expect_download
                    mock_btn.click.assert_called_once()
                    self.assertGreater(pdf_path.stat().st_size, MIN_PDF_SIZE_BYTES)

        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
