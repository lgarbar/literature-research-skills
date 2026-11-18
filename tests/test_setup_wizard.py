#!/usr/bin/env python3
"""Unit tests for the Interactive Setup Wizard."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import setup_wizard


class TestSetupWizard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.env_file = Path(self.temp_dir) / ".env"
        self.env_file.write_text("EXISTING_KEY=123\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_and_save_env_vars(self):
        env = setup_wizard.load_env_dict(self.env_file)
        self.assertEqual(env.get("EXISTING_KEY"), "123")

        setup_wizard.save_env_vars({
            "REFERENCE_MANAGER": "Zotero",
            "ZOTERO_USER_ID": "9876543",
            "ZOTERO_API_KEY": "secret_key_123",
            "ZOTERO_COLLECTION_NAME": "New",
            "PAPER_DOWNLOAD_DIR": "my_downloads"
        }, env_path=self.env_file)

        updated = setup_wizard.load_env_dict(self.env_file)
        self.assertEqual(updated.get("EXISTING_KEY"), "123")
        self.assertEqual(updated.get("REFERENCE_MANAGER"), "Zotero")
        self.assertEqual(updated.get("ZOTERO_USER_ID"), "9876543")
        self.assertEqual(updated.get("ZOTERO_API_KEY"), "secret_key_123")
        self.assertEqual(updated.get("ZOTERO_COLLECTION_NAME"), "New")
        self.assertEqual(updated.get("PAPER_DOWNLOAD_DIR"), "my_downloads")

    def test_mask_secret(self):
        self.assertEqual(setup_wizard.mask_secret(""), "")
        self.assertEqual(setup_wizard.mask_secret("short"), "*****")
        self.assertEqual(setup_wizard.mask_secret("1234567890abcdef"), "1234********cdef")

    @patch("builtins.input")
    def test_prompt_reference_manager_zotero(self, mock_input):
        # 1: Zotero, User ID, API Key, Collection
        mock_input.side_effect = ["1", "1234567", "my_zotero_key", "New"]
        with patch("setup_wizard.ENV_FILE", self.env_file):
            updates = setup_wizard.prompt_reference_manager()
            self.assertEqual(updates.get("REFERENCE_MANAGER"), "Zotero")
            self.assertEqual(updates.get("ZOTERO_USER_ID"), "1234567")
            self.assertEqual(updates.get("ZOTERO_API_KEY"), "my_zotero_key")
            self.assertEqual(updates.get("ZOTERO_COLLECTION_NAME"), "New")

    @patch("builtins.input")
    def test_prompt_reference_manager_paperpile(self, mock_input):
        mock_input.side_effect = ["2"]
        with patch("setup_wizard.ENV_FILE", self.env_file):
            updates = setup_wizard.prompt_reference_manager()
            self.assertEqual(updates.get("REFERENCE_MANAGER"), "Paperpile")

    @patch("builtins.input")
    def test_prompt_reference_manager_mendeley(self, mock_input):
        mock_input.side_effect = ["3"]
        with patch("setup_wizard.ENV_FILE", self.env_file):
            updates = setup_wizard.prompt_reference_manager()
            self.assertEqual(updates.get("REFERENCE_MANAGER"), "Mendeley")

    @patch("builtins.input")
    def test_prompt_reference_manager_endnote(self, mock_input):
        mock_input.side_effect = ["4"]
        with patch("setup_wizard.ENV_FILE", self.env_file):
            updates = setup_wizard.prompt_reference_manager()
            self.assertEqual(updates.get("REFERENCE_MANAGER"), "EndNote")

    @patch("builtins.input")
    def test_prompt_download_directory(self, mock_input):
        mock_input.side_effect = ["custom_papers"]
        with patch("setup_wizard.ENV_FILE", self.env_file):
            chosen = setup_wizard.prompt_download_directory()
            self.assertEqual(chosen, "custom_papers")
            env = setup_wizard.load_env_dict(self.env_file)
            self.assertEqual(env.get("PAPER_DOWNLOAD_DIR"), "custom_papers")

    def test_non_interactive_main(self):
        with patch("setup_wizard.ENV_FILE", self.env_file):
            with patch("sys.argv", ["setup_wizard.py", "--non-interactive"]):
                exit_code = setup_wizard.main()
                self.assertEqual(exit_code, 0)
                env = setup_wizard.load_env_dict(self.env_file)
                self.assertEqual(env.get("REFERENCE_MANAGER"), "Zotero")
                self.assertEqual(env.get("ZOTERO_COLLECTION_NAME"), "New")
                self.assertEqual(env.get("PAPER_DOWNLOAD_DIR"), "downloads")


if __name__ == "__main__":
    unittest.main()
