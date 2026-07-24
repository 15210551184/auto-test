"""
统一的 Chromium 启动参数。

四处需要起 Playwright 浏览器——scanner 扫描、runner 单页执行、batch 批量执行、
crawler 爬菜单——各自维护一份 launch 参数，容易漏改：scanner.scan() 曾经漏了
--no-sandbox，Docker 镜像默认以 root 运行，Chromium 缺这个参数直接起不来，
导致「扫描」在容器里必现失败而「执行」正常，表现像玄学 bug。
统一到这里，以后加一个参数只改一处，四个入口自动同步。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Playwright

LAUNCH_ARGS = [
    "--no-sandbox",                                     # 容器里默认 root 运行，Chromium 必须加这个才能起
    "--disable-dev-shm-usage",                          # 默认 /dev/shm 只有 64MB，大页面会崩
    "--disable-blink-features=AutomationControlled",    # 降低被业务系统识别为自动化的概率
]


def launch(pw: "Playwright", headless: bool = True, slow_mo: int = 0) -> "Browser":
    return pw.chromium.launch(headless=headless, slow_mo=slow_mo, args=LAUNCH_ARGS)


def context_args(**overrides: Any) -> Dict[str, Any]:
    """浏览器上下文的公共参数，调用方按需覆盖或追加（如 accept_downloads）。"""
    args: Dict[str, Any] = {
        "viewport": {"width": 1600, "height": 900},
        "locale": "zh-CN",
        "ignore_https_errors": True,
    }
    args.update(overrides)
    return args
