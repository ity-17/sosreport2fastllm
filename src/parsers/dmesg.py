"""dmesg 时间解析.

dmesg 使用相对时间格式 "[12345.678901] message..."，表示系统启动后 N 秒。
需要先获取 boot_time，再换算为 UNIX timestamp。
"""
import re
from pathlib import Path
from typing import Optional

DMESG_RE = re.compile(r'^\[\s*(\d+\.?\d*)\s*\](.*)')


def get_boot_time(sos_root: str) -> Optional[int]:
    """获取系统启动时间的 UNIX timestamp。4 级回退。"""
    root = Path(sos_root)

    # 方法1: /proc/stat 的 btime 字段
    proc_stat = root / "proc" / "stat"
    if proc_stat.exists():
        try:
            for line in open(proc_stat, errors="ignore"):
                if line.startswith("btime "):
                    return int(line.split()[1])
        except (IOError, ValueError):
            pass

    # 方法2: sos_commands 下的 who -b 输出
    for who_file in root.rglob("*who*"):
        if not who_file.is_file():
            continue
        try:
            for line in open(who_file, errors="ignore"):
                if "system boot" in line or "boot" in line.lower():
                    # "         system boot  Jul 18 15:30"
                    # 简化: 返回 None，回退到方法3
                    pass
        except IOError:
            pass

    # 方法3: messages 第一条 kernel 日志反推
    messages = root / "var" / "log" / "messages"
    if messages.exists():
        try:
            from src.parsers.syslog import parse_syslog_time
            for line in open(messages, errors="ignore"):
                if "kernel:" in line and ("Linux version" in line or "0.000000" in line):
                    ts = parse_syslog_time(line)
                    if ts:
                        return ts
        except IOError:
            pass

    # 方法4: 完全失败
    return None


def parse_dmesg_line(line: str, boot_time: int) -> Optional[dict]:
    """解析单行 dmesg 输出，返回 {timestamp, uptime_seconds, message}."""
    m = DMESG_RE.match(line)
    if not m:
        return None

    uptime = float(m.group(1))
    message = m.group(2).strip()
    timestamp = int(boot_time + uptime)

    return {
        "timestamp": timestamp,
        "uptime_seconds": uptime,
        "message": message,
    }
