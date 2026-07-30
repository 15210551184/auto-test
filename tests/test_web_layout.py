import unittest
from pathlib import Path


INDEX_HTML = (
    Path(__file__).resolve().parents[1] / "autotest" / "web" / "index.html"
).read_text(encoding="utf-8")


class RightColumnLayoutTests(unittest.TestCase):
    def test_operation_and_report_cards_have_layout_roles(self):
        self.assertIn('<div class="card ops-card">', INDEX_HTML)
        self.assertIn('<div class="card reports-card">', INDEX_HTML)

    def test_operation_card_cannot_squeeze_reports_out_of_view(self):
        self.assertIn(".col-right>.ops-card{", INDEX_HTML)
        self.assertIn("max-height:52%", INDEX_HTML)
        self.assertIn(".col-right>.reports-card{", INDEX_HTML)
        self.assertIn("min-height:220px", INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
