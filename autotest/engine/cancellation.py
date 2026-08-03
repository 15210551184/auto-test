"""执行任务的进程内优雅停止信号。"""

import threading


_requested = threading.Event()


def reset() -> None:
    _requested.clear()


def request() -> None:
    _requested.set()


def requested() -> bool:
    return _requested.is_set()
