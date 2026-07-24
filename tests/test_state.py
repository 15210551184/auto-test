import json
import tempfile
import unittest
from pathlib import Path

from autotest.engine.state import valid_storage_state


class StorageStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_state_is_ignored(self):
        self.assertIsNone(valid_storage_state(str(self.root / "missing.json")))

    def test_directory_state_is_ignored_with_warning(self):
        state = self.root / "auth.json"
        state.mkdir()
        warnings = []

        self.assertIsNone(valid_storage_state(str(state), warnings.append))
        self.assertIn("不是普通文件", warnings[0])

    def test_empty_and_malformed_state_are_ignored(self):
        empty = self.root / "empty.json"
        malformed = self.root / "malformed.json"
        empty.touch()
        malformed.write_text("not json", encoding="utf-8")

        self.assertIsNone(valid_storage_state(str(empty)))
        self.assertIsNone(valid_storage_state(str(malformed)))

    def test_valid_playwright_state_is_returned(self):
        state = self.root / "state.json"
        state.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

        self.assertEqual(str(state), valid_storage_state(str(state)))

    def test_json_object_without_playwright_collections_is_ignored(self):
        state = self.root / "wrong-shape.json"
        state.write_text(json.dumps({"token": "not-a-state"}), encoding="utf-8")

        self.assertIsNone(valid_storage_state(str(state)))
