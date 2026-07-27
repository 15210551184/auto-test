"""
测试数据工厂。

按字段类型和约束生成合法的测试值，供新增/修改用例自动填表。

**所有生成的值都带 auto_ 前缀 + 随机后缀**，两个目的：
  1. 一眼能看出是自动化造的数据，人在系统里看到不会误当成真实业务数据
  2. 唯一约束的字段不会因为重复跑而撞车

「只动自己建的数据」是这个工具的铁律：清理阶段靠这个前缀识别，
绝不删除或修改任何不是本次执行创建的记录。
"""
import random
import string
from datetime import timedelta
from typing import Any, Dict, List, Optional

from . import tz

AUTO_PREFIX = "auto_"

# 下拉/单选里的占位项，不是真实可选值
PLACEHOLDERS = {"全部", "请选择", "不限", "请输入", ""}

# 按字段名猜真实语义——光看控件类型不够，「加盟商联系方式」是 text 输入框，
# 但填随机字符串会被前端的手机号格式校验拦下来，得填合法手机号
_SEMANTIC = [
    (("手机", "电话", "联系方式", "phone", "mobile"), "phone"),
    (("邮箱", "email", "mail"), "email"),
    (("身份证", "idcard"), "idcard"),
    (("金额", "价格", "费用", "单价"), "money"),
    (("数量", "个数", "人数", "排序", "序号"), "int"),
    (("url", "链接", "网址"), "url"),
]


def _rand(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def semantic_of(label: str) -> Optional[str]:
    low = str(label).lower()
    for keys, kind in _SEMANTIC:
        if any(k in low for k in keys):
            return kind
    return None


def _by_semantic(kind: str) -> Optional[str]:
    if kind == "phone":
        return "138" + "".join(random.choices(string.digits, k=8))
    if kind == "email":
        return f"{AUTO_PREFIX}{_rand()}@example.com"
    if kind == "idcard":
        # 合法长度的测试身份证号，不保证校验位正确（多数系统只校长度/格式）
        return "11010119900101" + "".join(random.choices(string.digits, k=4))
    if kind == "money":
        return str(random.randint(1, 999)) + ".00"
    if kind == "int":
        return str(random.randint(1, 99))
    if kind == "url":
        return f"https://example.com/{_rand()}"
    return None


def real_options(options: Optional[List[str]]) -> List[str]:
    """滤掉「全部/请选择」这类占位项，留下真正能选的"""
    return [o for o in (options or []) if o and o.strip() not in PLACEHOLDERS]


def value_for(field: Dict[str, Any]) -> Optional[Any]:
    """
    给一个字段生成填写值。返回 None 表示这个字段填不了（上传、级联等），
    调用方应该跳过它并在报告里说明，而不是硬填一个错值。
    """
    if field.get("fillable") is False:
        return None
    ftype = field.get("type", "text")
    label = field.get("label", "")

    if ftype in ("select", "radio"):
        opts = real_options(field.get("options"))
        return opts[0] if opts else None
    if ftype == "checkbox":
        opts = real_options(field.get("options"))
        return opts[:1] if opts else None
    if ftype == "date":
        return f"{tz.now():%Y-%m-%d}"
    if ftype == "date_range":
        start = tz.now() - timedelta(days=7)
        return [f"{start:%Y-%m-%d}", f"{tz.now():%Y-%m-%d}"]
    if ftype == "switch":
        return True
    if ftype == "number":
        sem = semantic_of(label)
        return _by_semantic(sem) if sem in ("money", "int") else str(random.randint(1, 99))

    # 文本类：先看字段名有没有格式要求，没有才用通用随机串
    sem = semantic_of(label)
    if sem:
        val = _by_semantic(sem)
        if val:
            return _truncate(val, field.get("maxlength"))
    return _truncate(f"{AUTO_PREFIX}{_rand()}", field.get("maxlength"))


def _truncate(value: str, maxlength: Optional[int]) -> str:
    if maxlength and len(value) > maxlength:
        return value[:maxlength]
    return value


def fill_values(fields: List[Dict[str, Any]], only_required: bool = False) -> Dict[str, Any]:
    """
    给一组字段生成一份完整的填写值。
    only_required=True 时只填必填项，用来验证「非必填项留空也能提交成功」。
    """
    out = {}
    for f in fields:
        if only_required and not f.get("required"):
            continue
        v = value_for(f)
        if v is not None:
            out[f["label"]] = v
    return out


def overlong_value(field: Dict[str, Any]) -> Optional[str]:
    """生成一个超出 maxlength 的值，用来验证长度限制真的生效"""
    n = field.get("maxlength")
    if not n or field.get("type") not in ("text", "textarea"):
        return None
    return "A" * (int(n) + 10)


def is_auto_data(value: Any) -> bool:
    """这条数据是不是本工具造的——清理/删除前必须用它确认，绝不误删真实数据"""
    return AUTO_PREFIX in str(value)
