"""
统一时区：报告、报告文件夹命名里用到的时间戳都固定用北京时间。

不依赖部署环境的系统时区/TZ 环境变量——服务器裸机部署、精简 Docker 镜像
都可能没配对，之前就出现过报告时间和实际差 8 小时的情况。这里直接钉死
Asia/Shanghai，换到哪台机器上结果都一样。
"""
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    BEIJING = ZoneInfo("Asia/Shanghai")
except Exception:
    # 极少数精简镜像没装 IANA 时区数据库，退化成固定 UTC+8（东八区没有夏令时，等价）
    BEIJING = timezone(timedelta(hours=8))


def now() -> datetime:
    """当前北京时间，用于报告生成时间、报告文件夹命名。"""
    return datetime.now(BEIJING)


def from_ts(ts: float) -> datetime:
    """把 Unix 时间戳（如文件 mtime）转成北京时间。"""
    return datetime.fromtimestamp(ts, BEIJING)
