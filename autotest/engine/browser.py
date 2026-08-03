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
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-first-run",
]

# 扫描只读取 DOM、接口和控件能力，不需要下载图片、字体、音视频。正式执行
# 不启用此规则，因此报告截图、头像和图片列验证不受影响。
SCAN_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
SCAN_REQUIRED_IMAGE_HINTS = ("captcha", "verifycode", "validatecode", "verification")


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


def optimize_scan_context(context) -> None:
    """给菜单/页面结构扫描启用轻量网络策略；不用于正式测试执行。"""
    def handle(route):
        try:
            resource_type = route.request.resource_type
            url = route.request.url.lower()
        except Exception:
            resource_type = ""
            url = ""
        required_image = (resource_type == "image"
                          and any(hint in url for hint in SCAN_REQUIRED_IMAGE_HINTS))
        if resource_type in SCAN_BLOCKED_RESOURCE_TYPES and not required_image:
            route.abort()
        else:
            route.continue_()

    context.route("**/*", handle)
