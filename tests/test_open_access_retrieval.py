#!/usr/bin/env python3
"""Unit and integration tests for Open-Access detection, EZproxy bypass, and pipeline execution."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_and_process


class TestOpenAccessRetrieval(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.output_dir = Path(self.temp_dir) / "downloads"
        self.source_text_dir = Path(self.temp_dir) / "source-text"
        self.zotero_dir = Path(self.temp_dir) / "zotero_intake"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_known_open_access_doi_detection(self):
        # eLife
        is_oa, pub = fetch_and_process.is_known_open_access_doi("10.7554/eLife.09560")
        self.assertTrue(is_oa)
        self.assertEqual(pub, "eLife")

        # PLOS
        is_oa, pub = fetch_and_process.is_known_open_access_doi("10.1371/journal.pone.0210000")
        self.assertTrue(is_oa)
        self.assertEqual(pub, "PLOS")

        # Frontiers
        is_oa, pub = fetch_and_process.is_known_open_access_doi("10.3389/fnhum.2014.00490")
        self.assertTrue(is_oa)
        self.assertEqual(pub, "Frontiers")

        # arXiv
        is_oa, pub = fetch_and_process.is_known_open_access_doi("10.48550/arXiv.2301.12345")
        self.assertTrue(is_oa)
        self.assertEqual(pub, "arXiv")

        # PMC
        is_oa, pub = fetch_and_process.is_known_open_access_doi("PMC4559886")
        self.assertTrue(is_oa)
        self.assertEqual(pub, "PMC")

        # BioMed Central
        is_oa, pub = fetch_and_process.is_known_open_access_doi("10.1186/s12859-020-03823-w")
        self.assertTrue(is_oa)
        self.assertEqual(pub, "BioMed Central")

        # MDPI
        is_oa, pub = fetch_and_process.is_known_open_access_doi("10.3390/s21010001")
        self.assertTrue(is_oa)
        self.assertEqual(pub, "MDPI")

        # Non-OA publisher (e.g. Wiley)
        is_oa, pub = fetch_and_process.is_known_open_access_doi("10.1002/cphy.c170043")
        self.assertFalse(is_oa)
        self.assertIsNone(pub)

    def test_extract_elife_article_id(self):
        self.assertEqual(fetch_and_process.extract_elife_article_id("10.7554/eLife.09560"), "09560")
        self.assertEqual(fetch_and_process.extract_elife_article_id("10.7554/elife.27798.001"), "27798")
        self.assertEqual(fetch_and_process.extract_elife_article_id("10.7554/79301"), "79301")
        self.assertEqual(
            fetch_and_process.extract_elife_article_id(None, direct_url="https://elifesciences.org/articles/12345"),
            "12345"
        )

    def test_detect_and_resolve_elife_urls(self):
        doi = "10.7554/eLife.09560"
        with patch("fetch_and_process.query_crossref_oa_links", return_value=["https://cdn.elifesciences.org/articles/09560/elife-09560-v1.pdf"]):
            with patch("fetch_and_process.query_unpaywall_oa_links", return_value=[]):
                res = fetch_and_process.detect_and_resolve_oa(doi)
                self.assertTrue(res["is_oa"])
                self.assertEqual(res["publisher"], "eLife")
                # Direct canonical URL must be present
                self.assertIn("https://elifesciences.org/articles/09560.pdf", res["candidate_urls"])
                # Crossref CDN link must be present
                self.assertIn("https://cdn.elifesciences.org/articles/09560/elife-09560-v1.pdf", res["candidate_urls"])

    def test_detect_and_resolve_plos_urls(self):
        doi = "10.1371/journal.pone.0210000"
        with patch("fetch_and_process.query_crossref_oa_links", return_value=[]):
            with patch("fetch_and_process.query_unpaywall_oa_links", return_value=[]):
                res = fetch_and_process.detect_and_resolve_oa(doi)
                self.assertTrue(res["is_oa"])
                self.assertEqual(res["publisher"], "PLOS")
                self.assertIn(
                    "https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0210000&type=printable",
                    res["candidate_urls"]
                )

    def test_detect_and_resolve_frontiers_urls(self):
        doi = "10.3389/fnhum.2014.00490"
        with patch("fetch_and_process.query_crossref_oa_links", return_value=[]):
            with patch("fetch_and_process.query_unpaywall_oa_links", return_value=[]):
                res = fetch_and_process.detect_and_resolve_oa(doi)
                self.assertTrue(res["is_oa"])
                self.assertEqual(res["publisher"], "Frontiers")
                self.assertIn("https://www.frontiersin.org/articles/10.3389/fnhum.2014.00490/pdf", res["candidate_urls"])

    def test_detect_and_resolve_arxiv_urls(self):
        doi = "10.48550/arXiv.2301.12345"
        res = fetch_and_process.detect_and_resolve_oa(doi)
        self.assertTrue(res["is_oa"])
        self.assertEqual(res["publisher"], "arXiv")
        self.assertIn("https://arxiv.org/pdf/2301.12345.pdf", res["candidate_urls"])

    def test_detect_and_resolve_pmc_urls(self):
        res = fetch_and_process.detect_and_resolve_oa("PMC4559886")
        self.assertTrue(res["is_oa"])
        self.assertEqual(res["publisher"], "PMC")
        self.assertIn("https://pmc.ncbi.nlm.nih.gov/articles/PMC4559886/pdf/", res["candidate_urls"])

    def test_unpaywall_crossref_fallback_for_generic_doi(self):
        # A DOI not matching a hardcoded OA prefix, but confirmed OA via Unpaywall
        doi = "10.1038/s41467-020-00000"
        with patch("fetch_and_process.query_crossref_oa_links", return_value=[]):
            with patch("fetch_and_process.query_unpaywall_oa_links", return_value=["https://www.nature.com/articles/s41467-020-00000.pdf"]):
                res = fetch_and_process.detect_and_resolve_oa(doi)
                self.assertTrue(res["is_oa"])
                self.assertIn("https://www.nature.com/articles/s41467-020-00000.pdf", res["candidate_urls"])

    @patch("fetch_and_process.download_oa_via_http")
    def test_step_a_bypasses_ezproxy_and_sso_for_oa(self, mock_http):
        doi = "10.7554/eLife.09560"

        # Mock successful download
        def write_fake_pdf(urls, path):
            # Minimal valid PDF header and body
            path.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n" + b"A" * 150000)
            return True

        mock_http.side_effect = write_fake_pdf

        # Make sure no session file exists
        with patch("auth_setup.get_existing_state_path", return_value=None):
            with patch("fetch_and_process.source_files.inspect_file") as mock_inspect:
                mock_inspect.return_value = ({"mechanical_status": "pass", "bytes": 150050, "page_count": 5}, "")
                pdf_path, rep = fetch_and_process.step_a_download_paper(
                    doi=doi,
                    institution={"id": "vanderbilt", "name": "Vanderbilt University", "ezproxy_prefix": "https://proxy..."},
                    output_dir=self.output_dir
                )
                self.assertTrue(pdf_path.is_file())
                self.assertEqual(rep["mechanical_status"], "pass")
                self.assertTrue(mock_http.called)

    def test_step_a_requires_sso_for_non_oa_when_no_session(self):
        doi = "10.1002/cphy.c170043"  # Paywalled Wiley DOI
        with patch("fetch_and_process.query_crossref_oa_links", return_value=[]):
            with patch("fetch_and_process.query_unpaywall_oa_links", return_value=[]):
                with patch("auth_setup.get_existing_state_path", return_value=None):
                    with self.assertRaises(RuntimeError) as ctx:
                        fetch_and_process.step_a_download_paper(
                            doi=doi,
                            institution={"id": "vanderbilt", "name": "Vanderbilt University", "ezproxy_prefix": "https://proxy..."},
                            output_dir=self.output_dir
                        )
                    self.assertIn("No authenticated institutional session found", str(ctx.exception))

    @patch("fetch_and_process.step_a_download_paper")
    def test_full_pipeline_oa_sync_and_package(self, mock_step_a):
        doi = "10.1371/journal.pone.0210000"
        fake_pdf = self.output_dir / "10.1371_journal.pone.0210000.pdf"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        fake_pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n" + b"X" * 150000)

        mock_step_a.return_value = (fake_pdf, {"mechanical_status": "pass", "bytes": 150050, "page_count": 6})

        with patch("fetch_and_process.resolve_doi_metadata") as mock_meta:
            mock_meta.return_value = {
                "doi": doi,
                "title": "Ground reaction force metrics and tibial bone load",
                "authors": ["Author One", "Author Two"],
                "year": "2019",
                "venue": "PLOS ONE",
            }
            with patch("fetch_and_process.pymupdf4llm.to_markdown", return_value="# Ground reaction force\n\nFull text extracted here."):
                res = fetch_and_process.process_target(
                    doi=doi,
                    output_dir=self.output_dir,
                    source_text_dir=self.source_text_dir,
                    zotero_dir=self.zotero_dir,
                    headless=True
                )

                self.assertEqual(res["doi"], doi)
                self.assertIsNotNone(res["markdown_path"])
                md_path = Path(res["markdown_path"])
                self.assertTrue(md_path.is_file())
                md_content = md_path.read_text(encoding="utf-8")
                self.assertIn("Ground reaction force", md_content)
                self.assertIn('venue: "PLOS ONE"', md_content)

                # Step C verified Zotero intake package
                self.assertIsNotNone(res["zotero"])
                pkg_dir = Path(res["zotero"]["package_dir"])
                self.assertTrue(pkg_dir.is_dir())
                self.assertTrue((pkg_dir / "citation.ris").is_file())
                self.assertTrue((pkg_dir / "paper.pdf").is_file())
                self.assertTrue((pkg_dir / "notes.md").is_file())
                self.assertTrue((pkg_dir / "manifest.json").is_file())

    def test_resolve_zotero_collection_key_existing(self):
        mock_zot = MagicMock()
        mock_zot.all_collections.return_value = [
            {"key": "EXISTING123", "data": {"name": "New", "key": "EXISTING123"}},
            {"key": "OTHER456", "data": {"name": "Archive", "key": "OTHER456"}},
        ]
        key = fetch_and_process.resolve_zotero_collection_key(mock_zot, "New")
        self.assertEqual(key, "EXISTING123")
        mock_zot.create_collections.assert_not_called()

    def test_resolve_zotero_collection_key_creates_new(self):
        mock_zot = MagicMock()
        mock_zot.all_collections.return_value = [
            {"key": "OTHER456", "data": {"name": "Archive", "key": "OTHER456"}},
        ]
        mock_zot.create_collections.return_value = {
            "successful": {"0": {"key": "NEWLY_CREATED_KEY"}}
        }
        key = fetch_and_process.resolve_zotero_collection_key(mock_zot, "New")
        self.assertEqual(key, "NEWLY_CREATED_KEY")
        mock_zot.create_collections.assert_called_once_with([{"name": "New"}])

    def test_step_c_assigns_collection_directly_to_zotero_item(self):
        fake_pdf = self.output_dir / "paper.pdf"
        fake_md = self.source_text_dir / "paper.md"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.source_text_dir.mkdir(parents=True, exist_ok=True)
        fake_pdf.write_bytes(b"%PDF-1.4\n" + b"X" * 150000)
        fake_md.write_text("# Title\n\nContent", encoding="utf-8")

        mock_zot_instance = MagicMock()
        mock_zot_instance.all_collections.return_value = [
            {"key": "COLL_KEY_NEW", "data": {"name": "New", "key": "COLL_KEY_NEW"}}
        ]
        mock_zot_instance.item_template.return_value = {"itemType": "journalArticle", "title": ""}
        mock_zot_instance.create_items.return_value = {"successful": {"0": {"key": "ITEM123"}}}

        with patch.dict(os.environ, {"ZOTERO_API_KEY": "fake_key", "ZOTERO_USER_ID": "12345", "ZOTERO_COLLECTION_NAME": "New"}):
            with patch("fetch_and_process.zotero.Zotero", return_value=mock_zot_instance):
                res = fetch_and_process.step_c_sync_or_package_zotero(
                    pdf_path=fake_pdf,
                    markdown_path=fake_md,
                    metadata={"title": "Test Paper", "doi": "10.1234/test", "authors": ["Tester"]},
                    zotero_dir=self.zotero_dir,
                    collection_name="New"
                )

                self.assertEqual(res["mode"], "api_synced")
                self.assertEqual(res["item_key"], "ITEM123")
                self.assertEqual(res["collection"], "New")
                self.assertEqual(res["collection_key"], "COLL_KEY_NEW")

                # Verify collections assigned on created item
                created_payload = mock_zot_instance.create_items.call_args[0][0][0]
                self.assertIn("collections", created_payload)
                self.assertEqual(created_payload["collections"], ["COLL_KEY_NEW"])


if __name__ == "__main__":
    unittest.main()
