"""syslog 时间解析。

支持格式:
  "Jul 18 15:32:01 hostname message..."
  "Jan  5 03:01:59 hostname message..."  (单数字日期有前导空格)
"""
import re
from datetime import datetime
from typing import Optional

MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

SYSLOG_RE = re.compile(
    r'^(?P<month>\w{3})\s+(?P<day>\s?\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})'
)


def parse_syslog_time(line: str, year: Optional[int] = None) -> Optional[int]:
    """从 syslog 行提取 UNIX timestamp。"""
    m = SYSLOG_RE.match(line)
    if not m:
        return None

    month_name = m["month"].lower()
    if month_name not in MONTH_MAP:
        return None

    month = MONTH_MAP[month_name]
    day = int(m["day"].strip())
    time_parts = m["time"].split(":")
    hour, minute, second = int(time_parts[0]), int(time_parts[1]), int(time_parts[2])

    if year is None:
        year = datetime.now().year

    try:
        dt = datetime(year, month, day, hour, minute, second)
        return int(dt.timestamp())
    except ValueError:
        return None


def parse_syslog_line(line: str, year: Optional[int] = None) -> Optional[dict]:
    """解析整行 syslog，返回 {timestamp, host, message}."""
    ts = parse_syslog_time(line, year)
    if ts is None:
        return None

    # 提取 hostname（时间后的第一个词）
    rest = line[15:].strip()
    parts = rest.split(" ", 1)
    host = parts[0] if parts else ""
    message = parts[1] if len(parts) > 1 else ""

    return {"timestamp": ts, "host": host, "message": message}
