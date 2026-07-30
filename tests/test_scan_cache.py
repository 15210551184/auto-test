import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autotest.engine import batch


class ScanCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "page.json"
        self.url = "https://example.test/list"
        self.report = {
            "url": self.url,
            "title": "列表",
            "form_fields": [],
            "table": {"headers": ["名称"]},
            "buttons": {},
            "pagination": {},
            "list_api": "/api/list",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, crud=False, i18n=False, languages=None):
        batch._save_scan_cache(
            self.path, self.url, languages, self.report,
            include_crud=crud, include_i18n=i18n)

    def load(self, crud=False, i18n=False, languages=None):
        return batch._load_scan_cache(
            self.path, self.url, languages,
            include_crud=crud, include_i18n=i18n)

    def test_richer_cache_can_serve_lighter_generation(self):
        languages = {"scan_languages": ["en"]}
        self.save(crud=True, i18n=True, languages=languages)
        self.assertEqual(self.report, self.load())
        self.assertEqual(self.report, self.load(crud=True))
        self.assertEqual(self.report, self.load(i18n=True, languages=languages))

    def test_missing_capability_invalidates_cache(self):
        self.save(crud=False, i18n=False)
        self.assertIsNone(self.load(crud=True))
        self.assertIsNone(self.load(i18n=True))

    def test_language_change_invalidates_i18n_cache_only(self):
        self.save(i18n=True, languages={"scan_languages": ["en"]})
        changed = {"scan_languages": ["fr"]}
        self.assertIsNone(self.load(i18n=True, languages=changed))
        self.assertEqual(self.report, self.load(languages=changed))

    def test_url_version_and_malformed_cache_are_rejected(self):
        self.save()
        self.assertIsNone(batch._load_scan_cache(
            self.path, "https://example.test/other", None, False, False))

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["version"] = batch.SCAN_CACHE_VERSION + 1
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(self.load())

        self.path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(self.load())

    def test_regenerate_uses_cache_and_force_scan_bypasses_it(self):
        config = Path(self.tmp.name) / "pages" / "列表.yaml"
        cache = Path(self.tmp.name) / "scan-cache" / "列表.json"
        project = {"languages": None, "login": None}
        page = {"name": "列表", "url": self.url}
        generated = {"name": "列表", "url": self.url, "cases": []}

        with patch.object(batch.P, "load_project", return_value=project), \
             patch.object(batch.P, "selected_pages", return_value=[page]), \
             patch.object(batch.P, "page_config_path", return_value=config), \
             patch.object(batch.P, "scan_cache_path", return_value=cache), \
             patch.object(batch.P, "inject_project_settings",
                          side_effect=lambda cfg, _: cfg), \
             patch.object(batch.scanner, "to_config", return_value=generated), \
             patch.object(batch, "_scan_with_timeout",
                          return_value=self.report) as browser_scan:
            first = batch.scan_selected("demo", concurrency=1)
            second = batch.scan_selected("demo", overwrite=True, concurrency=1)
            forced = batch.scan_selected(
                "demo", overwrite=True, force_scan=True, concurrency=1)

        self.assertEqual(2, browser_scan.call_count)
        self.assertEqual(1, first["scanned"])
        self.assertEqual(1, second["cached"])
        self.assertEqual(1, forced["scanned"])


if __name__ == "__main__":
    unittest.main()
