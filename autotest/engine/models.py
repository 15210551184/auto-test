"""数据模型：配置结构 + 执行结果结构"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum


class Status(str, Enum):
    PASS = "pass"
    WARN = "warn"    # 检测到问题但不算失败（比如页面显示正常时的控制台报错）
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"


@dataclass
class Step:
    """单个动作或断言。action 是动作名，其余是参数。"""
    action: str
    params: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_raw(raw: Dict[str, Any]) -> "Step":
        # YAML 里每个 step 是 {action_name: params} 的单键字典
        if len(raw) != 1:
            raise ValueError(f"step 必须是单键字典，收到: {raw}")
        action, params = next(iter(raw.items()))
        if params is None:
            params = {}
        if not isinstance(params, dict):
            # 简写形式: {click: search_btn} -> {click: {target: search_btn}}
            params = {"value": params}
        return Step(action=action, params=params)


@dataclass
class Case:
    name: str
    steps: List[Step]
    tags: List[str] = field(default_factory=list)
    skip: bool = False


@dataclass
class PageConfig:
    name: str
    url: str
    selectors: Dict[str, str] = field(default_factory=dict)
    cases: List[Case] = field(default_factory=list)
    variables: Dict[str, Any] = field(default_factory=dict)
    list_api: Optional[str] = None          # 列表接口 URL 片段，用于等待响应
    export_mode: str = "auto"               # direct | async | auto
    export_task_api: Optional[str] = None
    login: Dict[str, Any] = field(default_factory=dict)   # UI 自动登录配置
    languages: Dict[str, Any] = field(default_factory=dict)   # 语言切换控件配置
    # 表单字段/表头列名的多语言文案："国家名称" 这个 canonical 名字继续用在
    # 所有 step 的 label/column 参数里（已有配置不用改），执行时按当前语言
    # 从这里查对应文案，查不到再挨个试——不需要知道当前是哪种语言。
    # 结构：{canonical: {lang_code: 对应文案}}
    label_variants: Dict[str, Dict[str, str]] = field(default_factory=dict)
    header_variants: Dict[str, Dict[str, str]] = field(default_factory=dict)


@dataclass
class StepResult:
    action: str
    params: Dict[str, Any]
    status: Status
    message: str = ""
    duration_ms: int = 0
    screenshot: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None


@dataclass
class CaseResult:
    name: str
    status: Status
    steps: List[StepResult] = field(default_factory=list)
    duration_ms: int = 0
    error: str = ""


@dataclass
class PageResult:
    name: str
    url: str
    cases: List[CaseResult] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.status in (Status.PASS, Status.WARN))

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if c.status in (Status.FAIL, Status.ERROR))
