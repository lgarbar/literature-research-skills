#!/usr/bin/env python3
"""
tests/test_format_print_fallback.py - Unit and Integration Tests for Token-Minimal Typesetting Fallback.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import format_print_fallback
import fetch_and_process


class TestDeterministicChromeStripping(unittest.TestCase):
    """Test deterministic regex stripping of repeating publisher web UI clutter."""

    def test_strip_reading_assistant_and_leapspace(self):
        dirty = """# Neural Mechanisms of Memory
Reading Assistant
powered by LeapSpace
Actions you could take:
Ask a question about this article
Summarize article

Here is the actual introductory text for the scientific paper."""
        cleaned = format_print_fallback.clean_publisher_chrome(dirty)
        self.assertNotIn("Reading Assistant", cleaned)
        self.assertNotIn("LeapSpace", cleaned)
        self.assertNotIn("Actions you could take", cleaned)
        self.assertNotIn("Ask a question", cleaned)
        self.assertIn("Neural Mechanisms of Memory", cleaned)
        self.assertIn("Here is the actual introductory text", cleaned)

    def test_strip_image_download_artifacts(self):
        dirty = """## Figure 1
Download: Download high-res image
Download full-size image (420KB)
Download figure
Open in figure viewer

Figure 1 shows neural firing patterns across the hippocampus."""
        cleaned = format_print_fallback.clean_publisher_chrome(dirty)
        self.assertNotIn("Download: Download high-res image", cleaned)
        self.assertNotIn("Download full-size image", cleaned)
        self.assertNotIn("Download figure", cleaned)
        self.assertNotIn("Open in figure viewer", cleaned)
        self.assertIn("Figure 1 shows neural firing patterns", cleaned)

    def test_strip_database_and_search_links(self):
        dirty = """Results were confirmed by prior studies.
View in Scopus
[Google Scholar](https://scholar.google.com/scholar?q=test)
View in Google Scholar
[Crossref](https://doi.org/10.1000/182)
[PubMed](https://pubmed.ncbi.nlm.nih.gov/12345/)

Next paragraph discussing statistical analysis."""
        cleaned = format_print_fallback.clean_publisher_chrome(dirty)
        self.assertNotIn("View in Scopus", cleaned)
        self.assertNotIn("scholar.google.com", cleaned)
        self.assertNotIn("View in Google Scholar", cleaned)
        self.assertNotIn("[Crossref]", cleaned)
        self.assertNotIn("[PubMed]", cleaned)
        self.assertIn("Results were confirmed by prior studies.", cleaned)
        self.assertIn("Next paragraph discussing statistical analysis.", cleaned)

    def test_deduplicate_repeated_box_blocks(self):
        dirty = """## Methodological Framework

Box 1 Key Methodological Concepts
This is the core conceptual framework for the study.

Box 1 Key Methodological Concepts
This is the core conceptual framework for the study.

## Results
Primary experimental results here."""
        cleaned = format_print_fallback.clean_publisher_chrome(dirty)
        count = cleaned.count("Box 1 Key Methodological Concepts")
        self.assertEqual(count, 1)
        self.assertIn("## Results", cleaned)
        self.assertIn("Primary experimental results here.", cleaned)


class TestReferenceSplitting(unittest.TestCase):
    """Test token-minimizing reference splitting so references are never sent to the LLM."""

    def test_split_references_standard_header(self):
        text = """# Paper Title
Here is the main text.

## References
[1] Author A. Journal, 2021.
[2] Author B. Science, 2022."""
        body, refs = format_print_fallback.split_markdown_at_references(text)
        self.assertIn("# Paper Title", body)
        self.assertIn("Here is the main text.", body)
        self.assertNotIn("[1] Author A", body)
        self.assertTrue(refs.startswith("## References"))
        self.assertIn("[1] Author A. Journal, 2021.", refs)
        self.assertIn("[2] Author B. Science, 2022.", refs)

    def test_split_literature_cited_and_bibliography(self):
        text1 = "# Paper\nBody text.\n\n### Literature Cited\n1. Ref one."
        body1, refs1 = format_print_fallback.split_markdown_at_references(text1)
        self.assertIn("Body text.", body1)
        self.assertIn("### Literature Cited", refs1)
        self.assertIn("1. Ref one.", refs1)

        text2 = "# Paper\nBody text.\n\n## Bibliography\n1. Ref two."
        body2, refs2 = format_print_fallback.split_markdown_at_references(text2)
        self.assertIn("Body text.", body2)
        self.assertIn("## Bibliography", refs2)

    def test_split_no_references(self):
        text = "# Short Communication\nOnly body text here."
        body, refs = format_print_fallback.split_markdown_at_references(text)
        self.assertEqual(body.strip(), text.strip())
        self.assertEqual(refs, "")


class TestOptionalLLMCleanup(unittest.TestCase):
    """Test token-minimal LLM cleanup behavior."""

    def setUp(self):
        self.original_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_default_zero_tokens_deterministic(self):
        os.environ.pop("ENABLE_LLM_FALLBACK_CLEANUP", None)
        dirty = "# Title\nReading Assistant\nBody text.\n## References\n[1] Ref 1"
        cleaned = format_print_fallback.reformat_with_optional_llm(dirty)
        self.assertNotIn("Reading Assistant", cleaned)
        self.assertIn("Body text.", cleaned)
        self.assertIn("## References", cleaned)

    @patch("httpx.post")
    def test_gemini_cleanup_excludes_references_and_reattaches(self, mock_post):
        os.environ["ENABLE_LLM_FALLBACK_CLEANUP"] = "true"
        os.environ["GEMINI_API_KEY"] = "fake-gemini-key"
        os.environ.pop("OPENAI_API_KEY", None)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "# Normalized Title\n\nCleaned body text without web artifacts."}]
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        sample_md = """# Raw Title
Reading Assistant
Body text with some artifacts.

## References
[1] Reference 1 (Should never be sent to LLM)
[2] Reference 2 (Should never be sent to LLM)"""

        result = format_print_fallback.reformat_with_optional_llm(sample_md)

        # Verify LLM request was made
        self.assertTrue(mock_post.called)
        sent_payload = mock_post.call_args[1]["json"]
        sent_text = sent_payload["contents"][0]["parts"][0]["text"]

        # Crucial token minimization requirement: references MUST NOT be sent to the LLM
        self.assertNotIn("Reference 1 (Should never be sent to LLM)", sent_text)
        self.assertNotIn("Reference 2 (Should never be sent to LLM)", sent_text)

        # References must be re-attached deterministically to the output
        self.assertIn("# Normalized Title", result)
        self.assertIn("Cleaned body text without web artifacts.", result)
        self.assertIn("## References", result)
        self.assertIn("[1] Reference 1 (Should never be sent to LLM)", result)

    @patch("httpx.post")
    def test_openai_cleanup_excludes_references_and_reattaches(self, mock_post):
        os.environ["ENABLE_LLM_FALLBACK_CLEANUP"] = "true"
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ["OPENAI_API_KEY"] = "fake-openai-key"

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "# Normalized OpenAI Title\n\nCleaned body text."
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        sample_md = """# Raw Title
View in Scopus
OpenAI Body text.

## References
[1] Excluded Reference"""

        result = format_print_fallback.reformat_with_optional_llm(sample_md)
        self.assertTrue(mock_post.called)
        sent_payload = mock_post.call_args[1]["json"]
        sent_content = sent_payload["messages"][1]["content"]

        self.assertNotIn("Excluded Reference", sent_content)
        self.assertIn("# Normalized OpenAI Title", result)
        self.assertIn("[1] Excluded Reference", result)

    @patch("httpx.post")
    def test_llm_failure_falls_back_to_deterministic_clean(self, mock_post):
        os.environ["ENABLE_LLM_FALLBACK_CLEANUP"] = "true"
        os.environ["GEMINI_API_KEY"] = "fake-key"
        mock_post.side_effect = Exception("Network timeout")

        sample_md = "# Title\nReading Assistant\nBody text.\n## References\n[1] Ref"
        result = format_print_fallback.reformat_with_optional_llm(sample_md)
        self.assertNotIn("Reading Assistant", result)
        self.assertIn("Body text.", result)
        self.assertIn("[1] Ref", result)


class TestJournalHTMLAndPDFGeneration(unittest.TestCase):
    """Test HTML layout styling and Playwright PDF export."""

    def test_markdown_to_journal_html_layout(self):
        md = """---
title: "Neural Basis of Cognitive Mapping"
doi: "10.1016/j.neuron.2024.01.001"
year: 2024
venue: "Neuron"
authors:
  - "Smith, Alice"
  - "Jones, Bob"
---

## Abstract
This study investigates cognitive mapping in mammalian cortex.

## Introduction
The hippocampal formation encodes spatial metrics.

> Key Finding: Grid cells exhibit 60-degree rotational symmetry.

| Parameter | Value |
| --- | --- |
| Resolution | 0.5 um |
| Sample Size | 48 |

## References
[1] O'Keefe, J. (1976). Place units in the hippocampus.
[2] Moser, E. (2005). Grid cells in entorhinal cortex."""

        html_out = format_print_fallback.markdown_to_journal_html(md)

        # 1. Subtle top banner requirement
        self.assertIn(
            "[Reformatted Print Capture - Not Publisher Typeset PDF]", html_out
        )
        self.assertIn("print-fallback-banner", html_out)

        # 2. Serif body typography
        self.assertIn("font-family: 'Charter'", html_out)

        # 3. Clean title and byline
        self.assertIn("Neural Basis of Cognitive Mapping", html_out)
        self.assertIn("Smith, Alice", html_out)
        self.assertIn("Jones, Bob", html_out)
        self.assertIn("Neuron", html_out)
        self.assertIn("10.1016/j.neuron.2024.01.001", html_out)

        # 4. Structured callout box & table
        self.assertIn("callout-box", html_out)
        self.assertIn("Key Finding: Grid cells", html_out)
        self.assertIn("styled-table", html_out)
        self.assertIn("Resolution", html_out)

        # 5. Two-column references layout
        self.assertIn("references-list", html_out)
        self.assertIn("column-count: 2", html_out)
        self.assertIn("O&#x27;Keefe, J.", html_out)

    def test_playwright_render_html_to_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_pdf = Path(tmpdir) / "output.pdf"
            sample_html = """<!DOCTYPE html>
<html>
<head><style>body { font-family: 'Charter', serif; }</style></head>
<body>
<div class="print-fallback-banner">[Reformatted Print Capture - Not Publisher Typeset PDF]</div>
<h1>Test Paper</h1>
<p>Test body paragraph.</p>
</body>
</html>"""
            format_print_fallback.render_html_to_pdf(sample_html, out_pdf)
            self.assertTrue(out_pdf.is_file())
            content = out_pdf.read_bytes()
            self.assertTrue(content.startswith(b"%PDF-"))
            self.assertGreater(len(content), 1000)

    def test_standalone_cli_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_file = Path(tmpdir) / "paper.md"
            out_pdf = Path(tmpdir) / "paper_formatted.pdf"

            md_file.write_text(
                """---
title: "CLI Test Article"
doi: "10.1016/test.cli"
year: 2025
venue: "Test Letters"
authors:
  - "Doe, Jane"
---
## Introduction
This is a test document formatted via standalone CLI execution.

## References
[1] Test Reference 2025.
""",
                encoding="utf-8",
            )

            cmd = [
                sys.executable,
                str(ROOT / "scripts" / "format_print_fallback.py"),
                str(md_file),
                "-o",
                str(out_pdf),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"CLI stderr: {res.stderr}")
            self.assertTrue(out_pdf.is_file())
            content = out_pdf.read_bytes()
            self.assertTrue(content.startswith(b"%PDF-"))


class TestPipelineIntegrationWithPrintFallback(unittest.TestCase):
    """Test fetch_and_process.py integration when is_print_fallback=True."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.downloads_dir = self.tmp_path / "downloads"
        self.source_text_dir = self.tmp_path / "working" / "source-text"
        self.zotero_dir = self.tmp_path / "working" / "zotero_intake"

        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.source_text_dir.mkdir(parents=True, exist_ok=True)
        self.zotero_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _create_minimal_pdf(self, path: Path, text: str = "Sample content"):
        # Generate valid PDF with Playwright
        html_src = f"<html><body><h1>Print Capture Paper</h1><p>Reading Assistant</p><p>powered by LeapSpace</p><p>{text}</p><p>Actions you could take:</p><p>Download: Download high-res image</p><h2>References</h2><p>[1] Ref 2024</p></body></html>"
        format_print_fallback.render_html_to_pdf(html_src, path)

    def test_pipeline_print_fallback_archival_and_formatted_zotero(self):
        doi = "10.1016/j.sample.2024.1001"
        safe_name = fetch_and_process.doi_to_filename(doi)
        raw_pdf_path = self.downloads_dir / f"{safe_name}.pdf"
        self._create_minimal_pdf(raw_pdf_path, text="Crucial scientific finding.")

        metadata = {
            "doi": doi,
            "title": "Sample Print Capture Article",
            "year": 2024,
            "venue": "Cell Press Fallback",
            "authors": ["Researcher, Alex"],
            "is_print_fallback": True,
        }

        # Mock step_a_download_paper returning print fallback report
        mock_report = {
            "is_print_fallback": True,
            "print_capture": True,
            "file_size": raw_pdf_path.stat().st_size,
        }

        with patch("fetch_and_process.step_a_download_paper", return_value=(raw_pdf_path, mock_report)), \
             patch("fetch_and_process.auth_setup.get_active_institution", return_value={"name": "Test Univ", "id": "test"}):

            res = fetch_and_process.process_target(
                doi=doi,
                output_dir=self.downloads_dir,
                source_text_dir=self.source_text_dir,
                zotero_dir=self.zotero_dir,
                skip_download=False,
                skip_oa=True,
            )

        # 1. Archival raw print capture must exist in downloads/
        archival_raw = self.downloads_dir / f"{safe_name}_raw_print.pdf"
        self.assertTrue(archival_raw.is_file(), "Archival raw print must be saved to downloads/{safe_doi}_raw_print.pdf")
        self.assertTrue(archival_raw.read_bytes().startswith(b"%PDF-"))

        # 2. Markdown conversion must have publisher chrome stripped
        md_file = self.source_text_dir / f"{safe_name}.md"
        self.assertTrue(md_file.is_file())
        md_content = md_file.read_text(encoding="utf-8")
        self.assertNotIn("Reading Assistant", md_content)
        self.assertNotIn("LeapSpace", md_content)
        self.assertNotIn("Download: Download high-res image", md_content)

        # 3. Clean journal-formatted PDF must be generated in working/zotero_intake/
        intake_package = self.zotero_dir / safe_name
        formatted_pdf = intake_package / "formatted_paper.pdf"
        paper_pdf = intake_package / "paper.pdf"

        self.assertTrue(formatted_pdf.is_file(), "Formatted PDF must be generated in zotero intake directory")
        self.assertTrue(paper_pdf.is_file(), "Intake paper.pdf must be packaged for Zotero")
        self.assertTrue(paper_pdf.read_bytes().startswith(b"%PDF-"))

        # 4. Manifest records intake and fallback tags
        manifest_file = intake_package / "manifest.json"
        self.assertTrue(manifest_file.is_file())
        import json
        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertTrue(manifest_data["is_print_fallback"])
        self.assertIn("print-capture-fallback", manifest_data["tags"])
        self.assertIn("raw_print_pdf", manifest_data["package_files"])


if __name__ == "__main__":
    unittest.main()
