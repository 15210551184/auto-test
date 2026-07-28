"""
字段 label / 表头列名的多语言文案解析。

label_variants / header_variants 的结构统一是 {canonical: {lang_code: 文案}}——
canonical 就是扫描时（默认语言下）拿到的原始文案，同时也是所有 case YAML
里 label/column 参数继续使用的值，已有配置格式不用改。

两个方向的查询：
- candidates()：canonical -> 所有已知文案（含它自己），"按文案定位元素"用，
  哪种语言不用关心，把所有候选一起交给 Playwright 找，命中哪个用哪个。
- reverse_map()：文案 -> canonical，"把读到的原始表头翻译回统一 key"用，
  这样 assert_column_all 这类断言永远按 canonical 名字取值，不用关心
  当前页面渲染的是哪种语言。
"""
from typing import Dict, List

Variants = Dict[str, Dict[str, str]]


def candidates(variants: Variants, canonical: str) -> List[str]:
    """canonical -> [canonical, 各语言译文...]，去重，canonical 排最前面。"""
    by_lang = (variants or {}).get(canonical) or {}
    out = [canonical]
    for text in by_lang.values():
        if text and text not in out:
            out.append(text)
    return out


def reverse_map(variants: Variants) -> Dict[str, str]:
    """所有已知文案（含 canonical 自己）-> canonical。"""
    out: Dict[str, str] = {}
    for canonical, by_lang in (variants or {}).items():
        out[canonical] = canonical
        for text in by_lang.values():
            if text:
                out[text] = canonical
    return out
