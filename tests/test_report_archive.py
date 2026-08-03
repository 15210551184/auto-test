import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from autotest.engine.report_archive import write_report_zip


class ReportArchiveTests(unittest.TestCase):
    def test_archives_complete_report_and_skips_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            report_dir = Path(temp) / "20260803_报告"
            (report_dir / "screenshots").mkdir(parents=True)
            (report_dir / "report.html").write_text("<h1>报告</h1>", encoding="utf-8")
            (report_dir / "screenshots" / "failed.png").write_bytes(b"png")

            outside = Path(temp) / "secret.txt"
            outside.write_text("secret", encoding="utf-8")
            (report_dir / "outside-link.txt").symlink_to(outside)

            output = io.BytesIO()
            write_report_zip(report_dir, report_dir.name, output)

            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    archive.namelist(),
                    [
                        "20260803_报告/report.html",
                        "20260803_报告/screenshots/failed.png",
                    ],
                )
                self.assertEqual(
                    archive.read("20260803_报告/report.html"),
                    "<h1>报告</h1>".encode(),
                )


if __name__ == "__main__":
    unittest.main()
