"""sosreport 解压 + 文件索引 + 类型识别."""
import tarfile
import re
from pathlib import Path
from typing import Optional
from src.models import FileEntry, FileManifest, LogType

# ============================================================
# 目录排除规则: 这些目录下的文件不会被读入 events 表
# ============================================================
SKIP_DIR_PREFIXES = [
    "proc/",              # /proc 快照，每个文件一行内核值，不是日志
    "sys/",               # /sys 快照
    "sos_reports/",       # sosreport 自己的 JSON/HTML 报告
    "sos_strings/",       # 二进制文件的 strings 输出
    "etc/selinux/",       # SELinux 策略文件（二进制 hex dump）
    "etc/udev/",          # udev 规则
    "var/lib/selinux/",   # SELinux 策略文件
    "var/lib/systemd/",   # systemd 目录（非文本日志）
]

# ============================================================
# 文件类型识别规则（按优先级从上到下匹配）
# ============================================================
FILE_TYPE_RULES: list[tuple[str, callable, LogType]] = [
    # SAR XML: sos_commands/sar/sa01.xml (sosreport 自动转换)
    ("sos_commands/sar/",
     lambda f: f.suffix == ".xml" and bool(re.match(r"sa\d+\.xml", f.name)),
     LogType.SAR_XML),

    # SAR 二进制: var/log/sa/sa01 (原始二进制)
    ("var/log/sa/",
     lambda f: bool(re.match(r"sa\d+$", f.name)),
     LogType.SAR_BINARY),
    ("sos_commands/sar/",
     lambda f: bool(re.match(r"sa\d+$", f.name)),
     LogType.SAR_BINARY),

    # 容器/Pod JSON 日志
    ("var/log/pods/",
     lambda f: f.suffix == ".log",
     LogType.CONTAINER_LOG),
    ("var/log/containers/",
     lambda f: f.suffix == ".log",
     LogType.CONTAINER_LOG),

    # 传统 syslog
    ("var/log/",
     lambda f: f.name in ("messages", "secure", "cron", "maillog", "spooler", "boot.log"),
     LogType.SYSLOG),
    ("var/log/",
     lambda f: f.name.startswith("messages-"),
     LogType.SYSLOG),
    ("var/log/audit/",
     lambda f: True,
     LogType.SYSLOG),

    # journal
    ("var/log/journal/",
     lambda f: f.suffix == ".journal",
     LogType.JOURNAL),

    # dmesg
    ("sos_commands/kernel/",
     lambda f: "dmesg" in f.name.lower(),
     LogType.DMESG),

    # sos_commands 下的文本输出（suffix 匹配）
    ("sos_commands/",
     lambda f: f.suffix in (".log", ".txt", ".out"),
     LogType.SYSLOG),
]

# ============================================================
# 优先级标记（数字越大越重要，timeline engine 优先处理）
# ============================================================
PRIORITY_PATTERNS = {
    3: [
        "var/log/messages",           # 核心系统日志
        "var/log/secure",             # 安全日志
        "var/log/cron",               # 定时任务
        "var/log/boot.log",           # 启动日志
        "var/log/audit/audit.log",    # 审计日志
        "sos_commands/kernel/dmesg",  # 内核环形缓冲区
        "var/log/sa/sa",              # SAR 二进制
        "sos_commands/sar/sa",        # SAR XML/二进制
    ],
    2: [
        "sos_commands/",              # 命令输出
        "var/log/",                   # 其他日志
    ],
    1: [
        "etc/",                       # 配置文件
    ],
}


# 白名单: 即使在 SKIP_DIR_PREFIXES 目录下也保留（dmesg boot_time 所需）
ALLOW_LIST = {"proc/stat"}


def _is_skip_path(rel_path: str) -> bool:
    """检查路径是否在排除目录中。支持相对路径和 tar 绝对路径（含 sosreport 根目录前缀）."""
    # 白名单优先
    for allowed in ALLOW_LIST:
        if rel_path.endswith(allowed) or rel_path.rstrip("/").endswith(allowed):
            return False
    for prefix in SKIP_DIR_PREFIXES:
        if rel_path.startswith(prefix):
            return True
        # 兼容 tar 成员路径: sosreport-xxx/proc/... → 匹配 proc/
        if "/" + prefix in "/" + rel_path.replace("\\", "/"):
            return True
    return False


def _classify_file(rel_path: str, file_path: Path) -> LogType:
    """根据路径规则判断文件类型."""
    # 先检查是否在排除目录中
    if _is_skip_path(rel_path):
        return LogType.UNKNOWN

    for prefix, check_fn, log_type in FILE_TYPE_RULES:
        if rel_path.startswith(prefix) and check_fn(file_path):
            return log_type

    # etc/ 下的无后缀文件 → UNKNOWN（配置文件，非时间序列日志）
    if rel_path.startswith("etc/") and file_path.suffix == "":
        return LogType.UNKNOWN

    # sos_commands/ 下的无后缀文件 → UNKNOWN（ps/netstat 等命令快照，非时间序列）
    if rel_path.startswith("sos_commands/") and file_path.suffix == "":
        return LogType.UNKNOWN

    # var/lib/ 下的无后缀文件 → UNKNOWN（SELinux 策略、chronyd drift 等非日志文件）
    if rel_path.startswith("var/lib/") and file_path.suffix == "":
        return LogType.UNKNOWN

    # 配置文件 → UNKNOWN（不读入 events）
    if file_path.suffix in (".conf", ".cfg", ".cnf", ".ini", ".yaml", ".yml", ".json", ".xml", ".html", ".csv"):
        return LogType.UNKNOWN

    # 无后缀的文本文件（通常是命令输出）→ SYSLOG
    if file_path.suffix == "" and file_path.stat().st_size < 10 * 1024 * 1024:  # <10MB
        return LogType.SYSLOG

    # 明确是日志后缀的
    if file_path.suffix in (".log", ".txt", ".out", ".err"):
        return LogType.SYSLOG

    return LogType.UNKNOWN


def _assign_priority(rel_path: str) -> int:
    """根据路径分配优先级."""
    for prio, patterns in sorted(PRIORITY_PATTERNS.items(), reverse=True):
        for pattern in patterns:
            if pattern in rel_path:
                return prio
    return 1


def scan_directory(sos_root: Path) -> FileManifest:
    """扫描已解压的 sosreport 目录，返回 FileManifest."""
    import os
    entries: list[FileEntry] = []
    sos_root = sos_root.resolve()

    for dirpath, dirnames, filenames in os.walk(sos_root, topdown=True, onerror=lambda e: None):
        # 跳过 SKIP_DIR_PREFIXES 中的目录（topdown=True + 原地修改 dirnames）
        dir_base = Path(dirpath)
        try:
            rel_dir = str(dir_base.relative_to(sos_root)).replace("\\", "/")
        except ValueError:
            continue
        if rel_dir == ".":
            rel_dir = ""

        before = len(dirnames)
        dirnames[:] = [
            d for d in dirnames
            if not _is_skip_path(f"{rel_dir}/{d}".lstrip("/"))
        ]
        # 同时跳过隐藏目录（.git 等）
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for fname in filenames:
            if fname.startswith("."):
                continue
            fpath = dir_base / fname
            rel_path = f"{rel_dir}/{fname}".lstrip("/")

            if _is_skip_path(rel_path):
                continue

            try:
                size = fpath.stat().st_size
            except OSError:
                continue

            log_type = _classify_file(rel_path, fpath)
            priority = _assign_priority(rel_path)

            entries.append(FileEntry(
                path=rel_path,
                abs_path=str(fpath),
                log_type=log_type,
                size_bytes=size,
                time_start=None,
                time_end=None,
                priority=priority,
            ))

    entries.sort(key=lambda e: (-e.priority, e.log_type.value))
    return FileManifest(total_files=len(entries), entries=entries)


def extract_and_index(archive_path: str, workspace: str = "./workspace") -> FileManifest:
    """解压 sosreport（或直接扫描目录）并返回 FileManifest."""
    import time
    archive = Path(archive_path)
    ws = Path(workspace)

    if archive.is_dir():
        t0 = time.time()
        manifest = scan_directory(archive)
        print(f"  Extract+index: {time.time() - t0:.1f}s")
        return manifest

    ws.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    # 逐文件解压，跳过不需要的目录（不解压到磁盘）
    mode = "r:*"
    extracted = 0
    skipped = 0
    skipped_dirs = 0
    with tarfile.open(archive, mode) as tar:
        members = tar.getmembers()
        print(f"  Archive contains {len(members)} entries, extracting...")
        for member in members:
            # 跳过非文件/目录；Windows 上跳过符号链接
            if not (member.isreg() or member.isdir()):
                skipped += 1
                continue
            if member.issym() or member.islnk():
                skipped += 1
                continue
            # 跳过不需要的目录（不解压 proc/, sys/, sos_reports/ 等）
            if _is_skip_path(member.name):
                skipped_dirs += 1
                continue
            try:
                tar.extract(member, path=ws, filter="fully_trusted")
                extracted += 1
            except (OSError, IOError):
                skipped += 1

    t1 = time.time()
    print(f"  Extracted {extracted} files (skipped {skipped} special + {skipped_dirs} unused dirs) in {t1 - t0:.1f}s")

    # 查找 sosreport 根目录
    sos_root = ws
    for d in sorted(ws.iterdir()):
        if d.is_dir() and (d / "sos_commands").is_dir():
            sos_root = d
            break

    manifest = scan_directory(sos_root)
    print(f"  Extract+index total: {time.time() - t0:.1f}s")
    return manifest
