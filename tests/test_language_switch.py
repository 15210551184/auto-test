import unittest

from autotest.engine.language_switch import default_language_code, switch_page_language


class Item:
    def __init__(self, visible=True, classes=""):
        self.visible = visible
        self.classes = classes
        self.clicked = 0

    @property
    def first(self):
        return self

    def is_visible(self):
        return self.visible

    def get_attribute(self, name):
        return self.classes if name == "class" else ""

    def click(self, timeout=None):
        self.clicked += 1

    def inner_text(self, timeout=None):
        return ""


class Matches:
    def __init__(self, items):
        self.items = items

    @property
    def first(self):
        return self.items[0]

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class Page:
    def __init__(self, items, trigger_text=""):
        self.trigger = Item()
        self.trigger.inner_text = lambda timeout=None: trigger_text
        self.matches = Matches(items)

    def locator(self, selector):
        return self.trigger

    def get_by_text(self, text, exact=False):
        return self.matches

    def wait_for_timeout(self, ms):
        pass


LANGUAGES = {"switcher_trigger": ".lang-select",
             "options": {"zh": "简体中文", "en": "英文"}}


class LanguageSwitchTests(unittest.TestCase):
    def test_chinese_is_the_stable_default_language(self):
        self.assertEqual("简体中文", default_language_code({
            "options": {"英文": "英文", "简体中文": "简体中文"},
        }))
        self.assertEqual("zh-CN", default_language_code({
            "options": {"en": "English", "zh-CN": "Simplified"},
        }))

    def test_trigger_already_shows_target_language(self):
        page = Page([], trigger_text="简体中文")
        self.assertFalse(switch_page_language(page, LANGUAGES, "zh"))
        self.assertEqual(0, page.trigger.clicked)

    def test_hidden_duplicate_is_skipped_and_visible_item_clicked(self):
        hidden, visible = Item(visible=False), Item(visible=True)
        page = Page([hidden, visible])
        self.assertTrue(switch_page_language(page, LANGUAGES, "en"))
        self.assertEqual(0, hidden.clicked)
        self.assertEqual(1, visible.clicked)

    def test_hidden_options_in_trigger_text_do_not_fake_current_language(self):
        visible = Item(visible=True)
        page = Page([visible], trigger_text="简体中文 英文 法语 阿拉伯语")
        self.assertTrue(switch_page_language(page, LANGUAGES, "en"))
        self.assertEqual(1, visible.clicked)

    def test_active_target_is_treated_as_already_selected(self):
        active = Item(visible=True, classes="el-dropdown-menu__item is-active")
        page = Page([active])
        self.assertFalse(switch_page_language(page, LANGUAGES, "zh"))
        self.assertEqual(0, active.clicked)
        self.assertEqual(2, page.trigger.clicked)


if __name__ == "__main__":
    unittest.main()
