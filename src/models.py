"""全局 Pydantic 数据模型。所有模块间传递的数据结构统一在此定义。"""
from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel


class LogType(str, Enum):
    SYSLOG = "syslog"
    JOURNAL = "journal"
    SAR_BINARY = "sar_binary"
    SAR_XML = "sar_xml"          # sosreport XML 格式 SAR（优先）
    DMESG = "dmesg"
    CONTAINER_LOG = "container_log"  # Kubernetes/容器 JSON 日志
    UNKNOWN = "unknown"


class FileEntry(BaseModel):
    """Extractor 输出的单个文件信息."""
    path: str
    abs_path: str
    log_type: LogType
    size_bytes: int
    time_start: Optional[int] = None
    time_end: Optional[int] = None
    priority: int = 1


class FileManifest(BaseModel):
    """Extractor 输出：完整的文件清单."""
    sos_hostname: str = ""
    sos_time: Optional[int] = None
    total_files: int
    entries: list[FileEntry]


class TimeEvent(BaseModel):
    """时间归一化后的单条事件."""
    timestamp: int
    source: str
    line: str
    normalized_time: str = ""


class RuleEvent(BaseModel):
    """规则引擎输出的单个异常事件."""
    event_type: str
    timestamp: Optional[int] = None
    severity: str = "MEDIUM"
    source_file: str = ""
    evidence: dict = {}


class StructuredEvidence(BaseModel):
    """规则引擎聚合输出 → 传给 RCA Agent."""
    fault_description: str
    fault_time: str
    time_window: tuple[int, int]
    events: list[RuleEvent] = []
    data_quality: str = "MEDIUM"


class RCAOutput(BaseModel):
    """RCA LLM Agent 输出."""
    root_cause: str
    summary: str
    timeline: list[str] = []
    evidence: list[str] = []
    impact: str = ""
    alternative_hypotheses: list[str] = []
    recommendations: list[str] = []
