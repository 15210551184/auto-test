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
import re
import unicodedata
from typing import Any, Dict, List, Sequence

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


_STOPWORDS = {
    # 英语 / 法语 / 阿拉伯语中不影响字段语义的冠词和介词。
    "a", "an", "the", "of", "de", "du", "des", "la", "le", "les", "d", "l",
    "من", "في", "على", "إلى",
}


def signature(value: Any) -> str:
    """生成跨语言宽松签名，用于匹配同一译文的轻微写法差异。"""
    plain = unicodedata.normalize("NFKD", str(value or ""))
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch)).lower()
    tokens = re.findall(r"[^\W_]+", plain, flags=re.UNICODE)
    normalized = []
    for token in tokens:
        if token in _STOPWORDS:
            continue
        # 阿拉伯语定冠词通常直接粘在名词前（例如 الحالة）。
        if token.startswith("ال") and len(token) > 4:
            token = token[2:]
        normalized.append(token)
    # 词序差异不改变表头含义：Country Code / Code of Country 应视为同一列。
    return " ".join(sorted(normalized))


def runtime_reverse_map(variants: Variants,
                        canonical_headers: Sequence[str] = (),
                        current_headers: Sequence[str] = ()) -> Dict[str, str]:
    """构建反向映射；旧配置缺翻译时可用等长页面表头安全补全。"""
    mapping = reverse_map(variants)
    if canonical_headers and len(canonical_headers) == len(current_headers):
        for translated, canonical in zip(current_headers, canonical_headers):
            if translated:
                mapping.setdefault(str(translated), str(canonical))
    return mapping


def canonical_name(value: Any, mapping: Dict[str, str]) -> Any:
    """映射回 canonical；宽松签名必须唯一命中，否则保留原值。"""
    if value in mapping:
        return mapping[value]
    wanted = signature(value)
    if not wanted:
        return value
    matches = {canonical for translated, canonical in mapping.items()
               if signature(translated) == wanted}
    return next(iter(matches)) if len(matches) == 1 else value


def canonicalize_row(row: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    """统一一行数据的列名；映射冲突时保留原列，避免静默覆盖数据。"""
    out: Dict[str, Any] = {}
    for key, value in row.items():
        canonical = canonical_name(key, mapping)
        target = canonical if canonical not in out else key
        out[target] = value
    return out
