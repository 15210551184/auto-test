"""
值归一化。

页面上是 "73.06 ¥"，Excel 里是 73.06，接口返回 7306（分）。
不做归一化，任何比对都会全量误报。这是整个工具最容易被低估的部分。
"""
import re
from datetime import datetime
from typing import Any, Optional

MONEY_RE = re.compile(r"-?[\d,]+\.?\d*")
DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日",
]

EMPTY_TOKENS = {"", "-", "--", "—", "无", "N/A", "null", "None", "暂无"}


def is_empty(v: Any) -> bool:
    return v is None or str(v).strip() in EMPTY_TOKENS


def money(v: Any) -> Optional[float]:
    """'73.06 ¥' / '¥1,234.50' / '2356.1' -> float"""
    if is_empty(v):
        return None
    m = MONEY_RE.search(str(v).replace(" ", ""))
    if not m:
        return None
    return round(float(m.group().replace(",", "")), 2)


def number(v: Any) -> Optional[float]:
    if is_empty(v):
        return None
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return float(s)
    except ValueError:
        return None


def date(v: Any) -> Optional[datetime]:
    """多格式日期解析。Excel 读出来可能已经是 datetime，直接放行。"""
    if is_empty(v):
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # 时间戳（秒 / 毫秒）
    if s.isdigit():
        ts = int(s)
        if ts > 1e11:
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError):
            return None
    return None


def text(v: Any) -> str:
    """去掉零宽字符、折叠空白、去省略号（表格列窄会截断成 '用户Bcl...'）"""
    if v is None:
        return ""
    s = str(v)
    s = re.sub(r"[\u200b-\u200f\ufeff]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def truncated(v: Any) -> bool:
    """页面值是否被 CSS/JS 截断，截断的值只能做前缀比对"""
    s = text(v)
    return s.endswith("...") or s.endswith("…")


def auto(v: Any):
    """猜类型：金额 > 日期 > 数字 > 文本"""
    s = text(v)
    if is_empty(s):
        return None
    if "¥" in s or "￥" in s or "$" in s:
        return money(s)
    d = date(s)
    if d:
        return d
    if re.fullmatch(r"-?[\d,]+\.?\d*", s):
        return number(s)
    return s


TYPE_MAP = {
    "money": money,
    "number": number,
    "date": date,
    "text": text,
    "auto": auto,
}


def compare(a: Any, b: Any, kind: str = "auto", tolerance: float = None) -> bool:
    """
    按类型比较两个值。

    金额默认容差 0.001（即必须精确到分）—— 1 分钱的差异正是这个工具要抓的
    舍入/单位 bug，容差设成 0.01 会把它放过去。
    页面值被截断时退化为前缀匹配：列宽截断是渲染行为，不算数据错误。
    """
    if tolerance is None:
        tolerance = 0.001 if kind == "money" else 0.01
    fn = TYPE_MAP.get(kind, auto)

    if kind in ("auto", "text") and truncated(a):
        prefix = text(a).rstrip(".…").rstrip()
        return text(b).startswith(prefix)
    if kind in ("auto", "text") and truncated(b):
        prefix = text(b).rstrip(".…").rstrip()
        return text(a).startswith(prefix)

    na, nb = fn(a), fn(b)
    if na is None and nb is None:
        return True
    if na is None or nb is None:
        return False
    if isinstance(na, float) and isinstance(nb, float):
        return abs(na - nb) <= tolerance
    if isinstance(na, datetime) and isinstance(nb, datetime):
        return abs((na - nb).total_seconds()) <= 1
    return text(na) == text(nb)
