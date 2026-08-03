import unittest

from autotest.engine.lang_variants import (
    candidates, canonical_name, canonicalize_row, reverse_map,
    runtime_reverse_map, signature,
)


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

    def test_signatures_handle_word_order_accents_and_articles(self):
        self.assertEqual(signature("Code pays"), signature("Code du pays"))
        self.assertEqual(signature("Country Code"), signature("Code of Country"))

    def test_runtime_map_only_uses_equal_length_headers(self):
        self.assertEqual(
            {"Code pays": "国家编码", "Nom pays": "国家名称"},
            runtime_reverse_map({}, ["国家编码", "国家名称"],
                                ["Code pays", "Nom pays"]),
        )
        self.assertEqual({}, runtime_reverse_map(
            {}, ["国家编码"], ["Code pays", "Nom pays"]))

    def test_fuzzy_mapping_requires_unique_match(self):
        mapping = {"Code pays": "国家编码"}
        self.assertEqual("国家编码", canonical_name("Code du pays", mapping))
        ambiguous = {"Country Code": "国家编码", "Code of Country": "区号"}
        self.assertEqual("Code Country", canonical_name("Code Country", ambiguous))

    def test_row_collision_does_not_overwrite_data(self):
        row = canonicalize_row(
            {"Code pays": "SN", "Code du pays": "CN"},
            {"Code pays": "国家编码"},
        )
        self.assertEqual("SN", row["国家编码"])
        self.assertEqual("CN", row["Code du pays"])

    def test_empty_variants_returns_empty_map(self):
        self.assertEqual({}, reverse_map({}))

    def test_untranslated_text_absent_from_map(self):
        rmap = reverse_map({"国家名称": {"en": "Country Name"}})
        self.assertNotIn("负责人", rmap)


if __name__ == "__main__":
    unittest.main()
