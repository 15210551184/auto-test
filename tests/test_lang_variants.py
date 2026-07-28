import unittest

from autotest.engine.lang_variants import candidates, reverse_map


class CandidatesTests(unittest.TestCase):
    def test_canonical_first_then_translations(self):
        variants = {"国家名称": {"en": "Country Name", "fr": "Nom du pays"}}
        out = candidates(variants, "国家名称")
        self.assertEqual("国家名称", out[0])
        self.assertIn("Country Name", out)
        self.assertIn("Nom du pays", out)
        self.assertEqual(3, len(out))

    def test_unknown_canonical_returns_itself_only(self):
        self.assertEqual(["负责人"], candidates({}, "负责人"))
        self.assertEqual(["负责人"], candidates({"国家名称": {"en": "x"}}, "负责人"))

    def test_duplicate_translation_not_repeated(self):
        # 极端情况：某语言译文碰巧和 canonical 一样
        variants = {"状态": {"en": "Status", "ja": "状态"}}
        out = candidates(variants, "状态")
        self.assertEqual(["状态", "Status"], out)

    def test_empty_translation_skipped(self):
        variants = {"国家名称": {"en": "Country Name", "fr": ""}}
        out = candidates(variants, "国家名称")
        self.assertNotIn("", out)


class ReverseMapTests(unittest.TestCase):
    def test_maps_every_translation_back_to_canonical(self):
        variants = {"国家名称": {"en": "Country Name", "fr": "Nom du pays"},
                    "状态": {"en": "Status"}}
        rmap = reverse_map(variants)
        self.assertEqual("国家名称", rmap["国家名称"])
        self.assertEqual("国家名称", rmap["Country Name"])
        self.assertEqual("国家名称", rmap["Nom du pays"])
        self.assertEqual("状态", rmap["Status"])

    def test_empty_variants_returns_empty_map(self):
        self.assertEqual({}, reverse_map({}))

    def test_untranslated_text_absent_from_map(self):
        rmap = reverse_map({"国家名称": {"en": "Country Name"}})
        self.assertNotIn("负责人", rmap)


if __name__ == "__main__":
    unittest.main()
