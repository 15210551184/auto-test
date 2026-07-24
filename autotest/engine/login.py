"""
UI 自动登录。

策略是「懒登录」：先用已存的 cookie 试目标页，发现被踢回登录页才重新登。
好处是不会在业务系统里堆登录日志，也不容易触发异地登录风控。

登录页的 DOM 写法各家差异很大，所以元素定位用多重兜底：
配置里指定的选择器 > 常见 name/placeholder > 表单里的位置推断。
"""
import os
import re
import time
from typing import List, Optional
from urllib.parse import urlparse

from playwright.sync_api import Page


# ---------- 候选选择器 ----------
# 按可靠性排序，逐个尝试。覆盖 Element UI / Ant Design / 原生表单。
USER_SELECTORS = [
    "input[name='username']", "input[name='userName']", "input[name='account']",
    "input[name='loginName']", "input[name='user']", "input[name='mobile']",
    "input[name='phone']", "input[name='email']",
    "input[placeholder*='用户名']", "input[placeholder*='账号']",
    "input[placeholder*='帐号']", "input[placeholder*='手机']",
    "input[placeholder*='邮箱']", "input[placeholder*='登录名']",
    "input[type='text']:visible", "input[type='email']:visible",
]

PASS_SELECTORS = [
    "input[type='password']",
    "input[name='password']", "input[name='passwd']", "input[name='pwd']",
    "input[placeholder*='密码']",
]

SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button:has-text('登 录')", "button:has-text('登录')", "button:has-text('登陆')",
    "button:has-text('登  录')",
    "a:has-text('登录')", "span:has-text('登录')",
    ".login-btn", ".el-button--primary", "button:has-text('Sign in')",
    "button:has-text('Login')",
]

# 判断"当前是不是在登录页"的特征
LOGIN_PAGE_HINTS = ["login", "signin", "sign-in", "auth", "passport"]


class LoginError(Exception):
    pass


def _first_visible(page: Page, selectors: List[str], timeout: int = 3000):
    """按顺序找第一个可见且可交互的元素"""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:
                continue
        page.wait_for_timeout(200)
    return None


def is_login_page(page: Page) -> bool:
    """
    当前页面是不是登录页。
    URL 特征 + 密码框存在，两个条件任一成立就算。
    密码框是最可靠的信号——业务页面几乎不会有 type=password。
    """
    url = page.url.lower()
    if any(h in url for h in LOGIN_PAGE_HINTS):
        return True
    try:
        pw = page.locator("input[type='password']")
        return pw.count() > 0 and pw.first.is_visible()
    except Exception:
        return False


def has_captcha(page: Page) -> Optional[str]:
    """
    检测验证码。有验证码就没法自动登录，早点报错比让用户等超时好。
    返回检测到的类型描述，没有则返回 None。
    """
    checks = [
        ("图形验证码", "input[placeholder*='验证码'], input[name*='captcha'], "
                    "input[name*='verifyCode'], img[src*='captcha'], img[src*='kaptcha']"),
        ("滑块验证", ".slide-verify, .nc-container, .geetest, [class*='slider-verify']"),
        ("短信验证码", "input[placeholder*='短信'], button:has-text('获取验证码')"),
    ]
    for name, sel in checks:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                return name
        except Exception:
            continue
    return None


def login(page: Page, login_url: str, username: str, password: str,
          user_selector: Optional[str] = None,
          pass_selector: Optional[str] = None,
          submit_selector: Optional[str] = None,
          success_url_contains: Optional[str] = None,
          timeout: int = 20000,
          extra_steps: Optional[list] = None) -> None:
    """
    在当前 page 上执行一次 UI 登录。

    user_selector / pass_selector / submit_selector 可在配置里指定，
    指定了就优先用，没指定走候选列表自动探测。
    """
    page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(800)

    cap = has_captcha(page)
    if cap:
        raise LoginError(
            f"登录页有{cap}，无法自动登录。\n"
            f"请改用本地登录方式：在有图形界面的机器上执行 "
            f"`python cli.py login <url>`，把生成的 auth/state.json 传到服务器。"
        )

    # --- 用户名 ---
    sels = ([user_selector] if user_selector else []) + USER_SELECTORS
    u = _first_visible(page, sels, timeout=5000)
    if u is None:
        raise LoginError(
            f"找不到用户名输入框。请在配置里手动指定 login.user_selector\n"
            f"（F12 选中输入框，右键 Copy → Copy selector）"
        )
    u.click()
    u.fill("")
    u.type(username, delay=30)

    # --- 密码 ---
    sels = ([pass_selector] if pass_selector else []) + PASS_SELECTORS
    p = _first_visible(page, sels, timeout=3000)
    if p is None:
        raise LoginError("找不到密码输入框。请在配置里手动指定 login.pass_selector")
    p.click()
    p.fill("")
    p.type(password, delay=30)

    # --- 额外步骤（比如需要勾选"记住我"、选择租户下拉等）---
    for step in (extra_steps or []):
        action, val = next(iter(step.items()))
        try:
            if action == "click":
                page.locator(val).first.click()
            elif action == "fill":
                page.locator(val["selector"]).first.fill(val["value"])
            elif action == "wait":
                page.wait_for_timeout(int(val))
        except Exception as e:
            raise LoginError(f"登录额外步骤 {action} 执行失败: {e}")

    # --- 提交 ---
    sels = ([submit_selector] if submit_selector else []) + SUBMIT_SELECTORS
    btn = _first_visible(page, sels, timeout=3000)
    if btn is None:
        # 兜底：密码框里回车，多数登录表单支持
        p.press("Enter")
    else:
        btn.click()

    # --- 等待登录完成 ---
    # 不能只等 URL 变化：有些系统登录失败也会跳转。
    # 判据是"离开了登录页" + "密码框消失"。
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        page.wait_for_timeout(500)
        if success_url_contains:
            if success_url_contains in page.url:
                break
        elif not is_login_page(page):
            break
    else:
        # 超时了，尝试把页面上的错误提示读出来，比"登录超时"有用得多
        msg = _read_error(page)
        raise LoginError(f"登录失败{'：' + msg if msg else '（超时，仍停留在登录页）'}")

    page.wait_for_timeout(1500)
    page.wait_for_timeout(500)


def _read_error(page: Page) -> str:
    """读登录页上的错误提示，如"账号或密码错误" """
    for sel in [".el-message--error", ".el-form-item__error", ".ant-message-error",
                ".error-msg", ".error", "[class*='error']:visible"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                t = loc.inner_text().strip()
                if t and len(t) < 100:
                    return t
        except Exception:
            continue
    return ""


def ensure_logged_in(page: Page, target_url: str, cfg: dict) -> bool:
    """
    懒登录：先试目标页，被踢回登录页才登录。

    返回 True 表示这次执行了登录动作，False 表示旧 cookie 还有效。

    判断"是否需要登录"有两个信号：
      1. 跳到了登录页（URL 特征或出现密码框）
      2. 配置了 expect_selector 但页面上找不到它
    第 2 条是为了覆盖"URL 没变但内容是未登录空壳"的单页应用——
    只看 URL 会漏，导致后面每条用例都在空页面上失败。
    """
    page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)

    need = is_login_page(page)

    # 目标页应该出现的元素，没出现说明没真正登录进去
    expect = cfg.get("expect_selector")
    if not need and expect:
        try:
            page.wait_for_selector(expect, timeout=cfg.get("expect_timeout", 5000))
        except Exception:
            need = True

    if not need:
        return False   # cookie 还有效

    username = _env_or(cfg.get("username"), "AUTOTEST_USER")
    password = _env_or(cfg.get("password"), "AUTOTEST_PASS")
    if not username or not password:
        raise LoginError(
            "登录态已失效，但没有配置账号密码。\n"
            "请设置环境变量 AUTOTEST_USER / AUTOTEST_PASS，"
            "或在配置的 login 段里填写。"
        )

    login_url = cfg.get("url") or page.url
    login(
        page, login_url, username, password,
        user_selector=cfg.get("user_selector"),
        pass_selector=cfg.get("pass_selector"),
        submit_selector=cfg.get("submit_selector"),
        success_url_contains=cfg.get("success_url_contains"),
        timeout=cfg.get("timeout", 20000),
        extra_steps=cfg.get("extra_steps"),
    )

    # 登录后回到目标页
    page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(800)
    if is_login_page(page):
        raise LoginError("登录后仍被重定向到登录页，可能是账号无该页面权限")
    return True


def _env_or(value: Optional[str], env_key: str) -> Optional[str]:
    """
    配置里可以写 ${env:VAR} 引用环境变量，避免密码进 git。
    没写配置就直接读默认环境变量名。
    """
    if not value:
        return os.environ.get(env_key)
    m = re.fullmatch(r"\$\{env:(\w+)\}", str(value).strip())
    if m:
        return os.environ.get(m.group(1))
    return value


def load_dotenv(path: str = ".env") -> None:
    """
    读 .env 文件到环境变量。
    cron 环境里读不到 shell 的 export，所以需要这个。
    已存在的环境变量优先，不覆盖。
    """
    if not os.path.isfile(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        os.environ.setdefault(k, v)
