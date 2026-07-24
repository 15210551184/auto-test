"""Playwright 登录态文件的安全读写。"""
import json
from pathlib import Path
from typing import Callable, Optional


def _warn(callback: Optional[Callable[[str], None]], message: str) -> None:
    if callback:
        callback(message)


def valid_storage_state(path: Optional[str],
                        on_warning: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """返回可交给 Playwright 的状态文件；无效文件则忽略并给出原因。"""
    if not path:
        return None
    state = Path(path)
    if not state.exists():
        return None
    if not state.is_file():
        _warn(on_warning, f"登录态已忽略：{path} 不是普通文件")
        return None
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _warn(on_warning, f"登录态已忽略：{path} 不是合法 JSON")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list) \
            or not isinstance(data.get("origins"), list):
        _warn(on_warning, f"登录态已忽略：{path} 不是 Playwright 登录态")
        return None
    return str(state)


def save_storage_state(context, path: Optional[str],
                       on_warning: Optional[Callable[[str], None]] = None) -> bool:
    """保存登录态；目录等无效目标只告警，不中断主任务。"""
    if not path:
        return False
    state = Path(path)
    if state.exists() and not state.is_file():
        _warn(on_warning, f"登录态未保存：{path} 不是普通文件")
        return False
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(state))
        return True
    except OSError as exc:
        _warn(on_warning, f"登录态未保存：{path}（{exc}）")
        return False
