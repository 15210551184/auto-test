"""执行任务的进程内优雅停止信号。"""

import threading
from typing import List

from .models import PageResult


_requested = threading.Event()
_results_lock = threading.Lock()
_completed_results: List[PageResult] = []


def reset() -> None:
    _requested.clear()
    reset_partial_results()


def request() -> None:
    _requested.set()


def requested() -> bool:
    return _requested.is_set()


def reset_partial_results() -> None:
    with _results_lock:
        _completed_results.clear()


def publish_partial_result(result: PageResult) -> None:
    with _results_lock:
        _completed_results.append(result)


def partial_results_snapshot() -> List[PageResult]:
    with _results_lock:
        return list(_completed_results)
