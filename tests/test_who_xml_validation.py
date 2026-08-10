from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from crawl_and_build_who_db import click_attached_control, find_chrome, validate_xml_export  # noqa: E402


class _FakeLocator:
    def __init__(self):
        self.wait_call = None
        self.evaluate_script = None

    def wait_for(self, **kwargs):
        self.wait_call = kwargs

    def evaluate(self, script):
        self.evaluate_script = script


class _FakePage:
    def __init__(self, locator):
        self._locator = locator

    def locator(self, selector):
        self.selector = selector
        return self._locator

class WhoXmlValidationTest(unittest.TestCase):
    def test_missing_system_browser_falls_back_to_playwright_managed_chromium(self):
        with mock.patch("crawl_and_build_who_db.chrome_candidates", return_value=[]):
            self.assertIsNone(find_chrome())

    def test_click_attached_control_triggers_hidden_webforms_control(self):
        locator = _FakeLocator()
        page = _FakePage(locator)
        click_attached_control(page, "#control", 180000)
        self.assertEqual(locator.wait_call, {"state": "attached", "timeout": 180000})
        self.assertEqual(locator.evaluate_script, "element => element.click()")
    def test_accepts_complete_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.xml"
            path.write_text("<Trials><Trial><TrialID>NCT00000001</TrialID></Trial></Trials>", encoding="utf-8")
            result = validate_xml_export(path, expected_trials=1)
            self.assertTrue(result["valid"])
            self.assertEqual(result["trial_records"], 1)

    def test_rejects_empty_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.xml"
            path.touch()
            self.assertEqual(validate_xml_export(path)["error"], "empty export")

    def test_rejects_export_shorter_than_portal_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.xml"
            path.write_text("<Trials><Trial><TrialID>NCT00000001</TrialID></Trial></Trials>", encoding="utf-8")
            result = validate_xml_export(path, expected_trials=2)
            self.assertFalse(result["valid"])
            self.assertIn("expected at least 2", result["error"])

    def test_rejects_malformed_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.xml"
            path.write_text("<Trials><Trial><TrialID>NCT00000001</TrialID>", encoding="utf-8")
            result = validate_xml_export(path)
            self.assertFalse(result["valid"])
            self.assertIn("no Trial records", result["error"])


if __name__ == "__main__":
    unittest.main()
