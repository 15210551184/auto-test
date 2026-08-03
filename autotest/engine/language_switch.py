"""扫描期和执行期共用的语言切换逻辑。"""

import re
from typing import Any, Dict, Optional


def default_language_code(languages: Dict[str, Any]) -> Optional[str]:
    """优先识别中文作为扫描和执行的默认语言。"""
    options = (languages or {}).get("options") or {}
    pattern = re.compile(r"简体中文|中文|chinese|zh[-_]?cn|^zh$", re.I)
    for code, text in options.items():
        if pattern.search(f"{code} {text}"):
            return code
    return None


def _visible_target(page, text: str):
    matches = page.get_by_text(text, exact=True)
    try:
        count = min(matches.count(), 12)
    except Exception:
        count = 1
    for index in range(count):
        try:
            item = matches.nth(index) if count > 1 else matches.first
            if not hasattr(item, "is_visible") or item.is_visible():
                return item
        except Exception:
            continue
    return None


def _is_selected_or_disabled(item) -> bool:
    attrs = {}
    for name in ("class", "aria-selected", "aria-disabled"):
        try:
            attrs[name] = item.get_attribute(name) or ""
        except Exception:
            attrs[name] = ""
    if attrs["aria-selected"].lower() == "true" or attrs["aria-disabled"].lower() == "true":
        return True
    classes = set(re.split(r"\s+", attrs["class"].strip()))
    return bool(classes & {"active", "selected", "disabled", "is-active",
                           "is-selected", "is-disabled"})


def switch_page_language(page, languages: Dict[str, Any], code: str,
                         current_code: Optional[str] = None) -> bool:
    """切换到目标语言；True 表示执行了点击，False 表示本来就是目标语言。"""
    options = (languages or {}).get("options") or {}
    if code not in options:
        raise LookupError(f"未知语言 '{code}'，配置里只有: {list(options)}")
    trigger_selector = (languages or {}).get("switcher_trigger")
    if not trigger_selector:
        raise LookupError("配置里没有 languages.switcher_trigger，无法切换语言")
    if current_code == code:
        return False

    target_text = str(options[code])
    trigger = page.locator(trigger_selector).first
    try:
        shown = (trigger.inner_text(timeout=500) or "").strip()
        if shown and target_text in shown:
            return False
    except Exception:
        pass

    trigger.click(timeout=5000)
    page.wait_for_timeout(300)
    target = _visible_target(page, target_text)
    if target is None:
        raise LookupError(f"语言菜单里找不到可见选项 '{target_text}'")
    if _is_selected_or_disabled(target):
        return False
    target.click(timeout=5000)
    page.wait_for_timeout(1000)
    return True
