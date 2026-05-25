"""关键词规则：匹配日志行中的关键模式，输出异常事件。

每条规则: (正则, event_type, severity)
"""
import re
from typing import Optional

KEYWORD_RULES: list[tuple[str, str, str]] = [
    # --- 内存 ---
    (r"Out of memory",                              "OOM_KILL",           "CRITICAL"),
    (r"invoked oom-killer",                         "OOM_KILL",           "CRITICAL"),
    (r"Killed process \d+",                         "PROCESS_KILLED",     "CRITICAL"),
    (r"Memory cgroup out of memory",                "CGROUP_OOM",         "CRITICAL"),

    # --- 内核 ---
    (r"blocked for more than \d+ seconds",          "BLOCKED_TASK",       "CRITICAL"),
    (r"task .* blocked",                            "BLOCKED_TASK",       "HIGH"),
    (r"rcu_sched detected stalls",                  "RCU_STALL",          "CRITICAL"),
    (r"soft lockup.*CPU.*stuck",                    "SOFT_LOCKUP",        "CRITICAL"),
    (r"BUG: soft lockup",                           "SOFT_LOCKUP",        "CRITICAL"),
    (r"Kernel panic",                               "KERNEL_PANIC",       "CRITICAL"),
    (r"BUG: unable to handle kernel",               "KERNEL_BUG",         "CRITICAL"),
    (r"hung_task",                                  "HUNG_TASK",          "HIGH"),

    # --- 存储 ---
    (r"Buffer I/O error",                           "IO_ERROR",           "CRITICAL"),
    (r"EXT4-fs error",                              "FILESYSTEM_ERROR",   "CRITICAL"),
    (r"XFS.*error",                                 "FILESYSTEM_ERROR",   "CRITICAL"),
    (r"Read-only file system",                      "FS_READONLY",        "CRITICAL"),
    (r"journal commit I/O error",                   "JOURNAL_ERROR",      "CRITICAL"),
    (r"multipath.*fail",                            "MULTIPATH_FAIL",      "HIGH"),
    (r"Remounting filesystem read-only",             "FS_REMOUNT_RO",      "CRITICAL"),

    # --- 网络 ---
    (r"possible SYN flooding",                      "SYN_FLOOD",          "HIGH"),
    (r"net_ratelimit.*callbacks suppressed",         "NET_RATELIMIT",      "MEDIUM"),
    (r"nf_conntrack: table full",                   "CONNTRACK_FULL",     "HIGH"),

    # --- systemd ---
    (r"Failed to start .*\.service",                "SERVICE_FAILED",     "HIGH"),
    (r"Main process exited.*killed",                 "SERVICE_KILLED",     "CRITICAL"),

    # --- 其他关键事件 ---
    (r"Unhandled error code",                       "STORAGE_ERROR",      "CRITICAL"),
    (r"Result: hostbyte=DID_ERROR",                 "STORAGE_ERROR",      "CRITICAL"),
    (r"end_request: I/O error",                     "IO_ERROR",           "CRITICAL"),
    (r"state is now lost",                          "NODE_LOST",          "CRITICAL"),
    (r"TOTEM.*Retransmit List",                     "COROSYNC_RETRANS",   "HIGH"),
]

# 编译正则
COMPILED_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(pattern, re.IGNORECASE), event_type, severity)
    for pattern, event_type, severity in KEYWORD_RULES
]


def match_keyword_rules(line: str) -> list[tuple[str, str]]:
    """对单行日志匹配所有关键词规则。

    返回: [(event_type, severity), ...] 一行可能匹配多条规则
    """
    matches = []
    for pattern, event_type, severity in COMPILED_RULES:
        if pattern.search(line):
            matches.append((event_type, severity))
    return matches
