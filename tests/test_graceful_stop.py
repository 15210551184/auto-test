import json
import tempfile
import unittest
from pathlib import Path

from autotest.engine import cancellation
from autotest.engine.models import CaseResult, PageResult, Status
from autotest.engine.report import render, render_meta


class GracefulStopTests(unittest.TestCase):
    def tearDown(self):
        cancellation.reset()

    def test_cancellation_flag_can_be_requested_and_reset(self):
        cancellation.reset()
        self.assertFalse(cancellation.requested())
        cancellation.request()
        self.assertTrue(cancellation.requested())
        cancellation.reset()
        self.assertFalse(cancellation.requested())

    def test_partial_report_is_marked_as_stopped(self):
        result = PageResult("国家管理", "http://example.test", [
            CaseResult("已完成用例", Status.PASS),
        ])
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "report.html"
            render([result], str(out), stopped=True)
            text = out.read_text(encoding="utf-8")
        self.assertIn("任务已由用户中途停止", text)
        self.assertIn("已完成用例", text)

    def test_report_meta_records_stopped_state(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "meta.json"
            render_meta(str(out), page_count=1, stopped=True)
            meta = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(meta["stopped"])


if __name__ == "__main__":
    unittest.main()
