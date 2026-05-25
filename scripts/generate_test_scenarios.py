"""生成 4 个测试 sosreport tar.xz 文件，覆盖 keyword + threshold rules。

SAR XML 数据确保 data_quality 达到 HIGH（4/4 数据源通过）。
直接写入 tar.xz，不创建中间目录。

用法: python scripts/generate_test_scenarios.py
输出: test_scenarios/{io_hang_oom,memory_exhaustion,cpu_softlockup,filesystem_corruption}.tar.xz
"""
import io
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

MEMTOTAL_KB = 16777216


@dataclass
class Scenario:
    name: str
    date: str           # "2024-07-18"
    fault_hour: int
    fault_minute: int
    boot_hour: int
    boot_minute: int
    hostname: str = "host1"
    # Messages: list of (offset_minutes_from_fault, message_line)
    messages: list = field(default_factory=list)
    # Dmesg: list of (offset_seconds_from_fault, message_line)
    dmesg_lines: list = field(default_factory=list)
    # SAR metrics overrides at fault time: dict of metric->value for the spike
    sar_spike: dict = field(default_factory=dict)

    @property
    def boot_dt(self) -> datetime:
        d = datetime.strptime(self.date, "%Y-%m-%d")
        return d.replace(hour=self.boot_hour, minute=self.boot_minute, second=0)

    @property
    def fault_dt(self) -> datetime:
        d = datetime.strptime(self.date, "%Y-%m-%d")
        return d.replace(hour=self.fault_hour, minute=self.fault_minute, second=0)

    @property
    def boot_ts(self) -> int:
        return int(self.boot_dt.timestamp())

    def dt_at(self, offset_min: float) -> datetime:
        return self.fault_dt + timedelta(minutes=offset_min)

    def syslog_time(self, dt: datetime) -> str:
        month = MONTHS[dt.month - 1]
        return f"{month} {dt.day:>2} {dt.strftime('%H:%M:%S')}"

    def tar_prefix(self) -> str:
        date_str = self.date.replace("-", "")
        return f"sosreport-mock-{self.name}-{date_str}"


def _build_messages(s: Scenario) -> str:
    """Build var/log/messages content."""
    lines = []
    # First line: kernel boot message (fallback for boot_time detection method 3)
    boot_line = (f"{s.syslog_time(s.boot_dt)} {s.hostname} kernel: Linux version "
                 f"4.18.0-mock (mockbuild@build) (gcc version 8.5.0) #1 SMP "
                 f"{s.date}")
    lines.append(boot_line)

    for offset_min, msg in sorted(s.messages):
        dt = s.dt_at(offset_min)
        lines.append(f"{s.syslog_time(dt)} {s.hostname} {msg}")

    return "\n".join(lines) + "\n"


def _build_dmesg(s: Scenario) -> str:
    """Build sos_commands/kernel/dmesg content."""
    lines = []
    # First line: kernel version banner
    uptime_0 = (s.boot_dt - s.boot_dt).total_seconds()
    lines.append(f"[{uptime_0:>12.6f}] Linux version 4.18.0-mock (mockbuild@build)")
    lines.append(f"[{uptime_0 + 1.0:>12.6f}] Command line: ro root=/dev/sda1 quiet")

    for offset_sec, msg in sorted(s.dmesg_lines):
        # uptime = event_time - boot_time
        event_dt = s.fault_dt + timedelta(seconds=offset_sec)
        uptime = (event_dt - s.boot_dt).total_seconds()
        lines.append(f"[{uptime:>12.6f}] {msg}")

    return "\n".join(lines) + "\n"


def _build_sar_xml(s: Scenario) -> str:
    """Build sos_commands/sar/sa01.xml with metric spikes at fault time."""
    ns = "http://pagesperso-orange.fr/sebastien.godard/sysstat"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}sysstat")

    # Host element
    host_elem = ET.SubElement(root, f"{{{ns}}}host")
    host_elem.set("nodename", s.hostname)

    # Generate timestamps every 5 minutes from T-30min to T+30min
    for offset in range(-30, 31, 5):
        dt = s.dt_at(offset)
        ts_elem = ET.SubElement(root, f"{{{ns}}}timestamp")
        ts_elem.set("date", dt.strftime("%Y-%m-%d"))
        ts_elem.set("time", dt.strftime("%H:%M:%S"))
        ts_elem.set("utc", "0")

        # Determine if this is the fault spike window
        is_spike = offset in (-5, 0, 5)

        # --- CPU ---
        cpu_load = ET.SubElement(ts_elem, f"{{{ns}}}cpu-load")
        cpu = ET.SubElement(cpu_load, f"{{{ns}}}cpu")
        cpu.set("number", "all")

        # Normal CPU values
        cpu_user = 5.0 + (abs(offset) * 0.1)
        cpu_system = 3.0 + (abs(offset) * 0.05)
        cpu_iowait = s.sar_spike.get("cpu_iowait", 2.0) if is_spike else 1.0
        cpu_steal = s.sar_spike.get("cpu_steal", 0.0) if is_spike else 0.0
        cpu_idle = 100.0 - cpu_user - cpu_system - cpu_iowait - cpu_steal

        cpu.set("user", f"{cpu_user:.2f}")
        cpu.set("nice", "0.00")
        cpu.set("system", f"{cpu_system:.2f}")
        cpu.set("iowait", f"{cpu_iowait:.2f}")
        cpu.set("steal", f"{cpu_steal:.2f}")
        cpu.set("idle", f"{max(0, cpu_idle):.2f}")
        cpu.set("softirq", "0.50")
        cpu.set("irq", "0.25")

        # --- Memory ---
        mem = ET.SubElement(ts_elem, f"{{{ns}}}memory")
        memfree = s.sar_spike.get("memfree", MEMTOTAL_KB * 0.30) if is_spike else MEMTOTAL_KB * 0.40
        memused = MEMTOTAL_KB - memfree
        swapused = s.sar_spike.get("swapused", MEMTOTAL_KB * 0.10) if is_spike else MEMTOTAL_KB * 0.05

        mem.set("memfree", f"{memfree:.0f}")
        mem.set("memused", f"{memused:.0f}")
        mem.set("memused-percent", f"{memused / MEMTOTAL_KB * 100:.2f}")
        mem.set("swapfree", f"{MEMTOTAL_KB * 0.5 - swapused:.0f}")
        mem.set("swapused", f"{swapused:.0f}")
        mem.set("buffers", f"{MEMTOTAL_KB * 0.02:.0f}")
        mem.set("cached", f"{MEMTOTAL_KB * 0.15:.0f}")
        mem.set("commit", f"{MEMTOTAL_KB * 0.8:.0f}")
        mem.set("commit-percent", "80.00")
        mem.set("active", f"{MEMTOTAL_KB * 0.25:.0f}")
        mem.set("inactive", f"{MEMTOTAL_KB * 0.10:.0f}")

        # --- Disk ---
        disk = ET.SubElement(ts_elem, f"{{{ns}}}disk")
        disk.set("device", "sda")
        disk_await = s.sar_spike.get("io_await", 200.0) if is_spike else 50.0
        disk_util = s.sar_spike.get("io_util", 30.0) if is_spike else 5.0

        disk.set("await", f"{disk_await:.2f}")
        disk.set("util", f"{disk_util:.2f}")
        disk.set("svctm", "1.50")
        disk.set("avgqu-sz", "2.00")
        disk.set("avgrq-sz", "64.00")
        disk.set("r_await", f"{disk_await * 0.5:.2f}")
        disk.set("w_await", f"{disk_await:.2f}")
        disk.set("rkB", "1024.00")
        disk.set("wkB", "2048.00")
        disk.set("tps", "150.00")

        # --- Network ---
        net = ET.SubElement(ts_elem, f"{{{ns}}}net-dev")
        net.set("iface", "eth0")
        net.set("rxdrop", "0.00")
        net.set("txdrop", "0.00")
        net.set("rxerr", "0.00")
        net.set("txerr", "0.00")
        net.set("rxpck", "5000.00")
        net.set("txpck", "4000.00")
        net.set("rxkB", "20000.00")
        net.set("txkB", "15000.00")

        # --- Load average ---
        la = ET.SubElement(ts_elem, f"{{{ns}}}load-average")
        la.set("load1", "2.50" if not is_spike else f"{s.sar_spike.get('load1', 50):.2f}")
        la.set("load5", "2.00" if not is_spike else "40.00")
        la.set("load15", "1.50" if not is_spike else "25.00")

    return ET.tostring(root, encoding="unicode")


def _build_proc_stat(s: Scenario) -> str:
    """Build proc/stat containing btime."""
    return (
        "cpu  1123456 12345 987654 98765432 12345 0 1234 0 0 0\n"
        "cpu0 561728 6172 493827 49382716 6172 0 617 0 0 0\n"
        "cpu1 561728 6173 493827 49382716 6173 0 617 0 0 0\n"
        "intr 1234567890 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
        f"ctxt 9876543210\n"
        f"btime {s.boot_ts}\n"
        "processes 123456\n"
        "procs_running 5\n"
        "procs_blocked 12\n"
        "softirq 1234567890 0 0 0 0 0 0 0 0 0 0 0\n"
    )


def _build_fstab(s: Scenario) -> str:
    return (
        "/dev/sda1 / ext4 defaults,noatime 1 1\n"
        "/dev/sda2 /data ext4 defaults 0 0\n"
    )


def build_tar(s: Scenario, output_dir: Path):
    """Create tar.xz with all scenario files directly in memory."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{s.name}.tar.xz"
    prefix = s.tar_prefix()

    files = {
        f"{prefix}/var/log/messages": _build_messages(s),
        f"{prefix}/sos_commands/kernel/dmesg": _build_dmesg(s),
        f"{prefix}/sos_commands/sar/sa01.xml": _build_sar_xml(s),
        f"{prefix}/proc/stat": _build_proc_stat(s),
        f"{prefix}/etc/fstab": _build_fstab(s),
    }

    with tarfile.open(str(out_path), "w:xz") as tar:
        for arcname, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            info.mtime = int(s.fault_dt.timestamp())
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))

    # Report size
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"  Created: {out_path.name} ({size_mb:.2f} MB)")
    return out_path


# ============================================================
# Scenario Definitions
# ============================================================

def _fmt_msg(ts: str, msg: str) -> str:
    """Helper: format message part of syslog line."""
    return msg


def define_scenarios() -> list[Scenario]:
    scenarios = []

    # ================================================================
    # Scenario 1: IO Hang → OOM Kill
    # Date: 2026-05-20, Fault: 15:32, Boot: 12:00
    # ================================================================
    s1 = Scenario(
        name="io_hang_oom",
        date="2026-05-20",
        fault_hour=15, fault_minute=32,
        boot_hour=12, boot_minute=0,
        sar_spike={
            "cpu_iowait": 85.0,      # >80 → EXTREME_IOWAIT CRITICAL, >50 → HIGH_IOWAIT HIGH
            "io_await": 8000.0,      # >5000 → IO_LATENCY CRITICAL
            "io_util": 95.0,         # >90 → IO_SATURATION CRITICAL
            "memfree": MEMTOTAL_KB * 0.06,    # memused ≈ 94% → MEMORY_PRESSURE HIGH (>90)
            "swapused": MEMTOTAL_KB * 0.30,   # well under 50%
            "load1": 88.0,
        },
    )
    s1.messages = [
        # Normal period (15:17 - 15:29)
        (-15, "systemd: Starting Session 42 of user root."),
        (-13, "sshd[2841]: Accepted publickey for root from 10.0.1.100 port 52341."),
        (-11, "CRON[2892]: (root) CMD (run-parts /etc/cron.hourly)"),
        (-9, "systemd: Started Session 43 of user root."),
        (-7, "sshd[2901]: Accepted publickey for root from 10.0.1.101 port 52342."),
        (-5, "CRON[2910]: (root) CMD (/usr/lib/sa/sa1 1 1)"),
        # Precursor (15:30 - 15:31)
        (-2, 'kernel: INFO: task jbd2/sda1-8:1234 blocked for more than 120 seconds.'),
        (-1.5, 'kernel: Buffer I/O error on device sda1, logical block 123456.'),
        (-1.5, 'multipathd: sda: add missing path.'),
        (-1, 'kernel: sd 0:0:0:0: [sda] Unhandled error code.'),
        (-1, 'kernel: sd 0:0:0:0: [sda] Result: hostbyte=DID_ERROR driverbyte=DRIVER_OK.'),
        (-0.8, 'kernel: end_request: I/O error, dev sda, sector 19126016.'),
        (-0.5, 'kernel: EXT4-fs error (device sda1): ext4_journal_start_sb: Detected aborted journal.'),
        (-0.3, 'kernel: Remounting filesystem read-only.'),
        # Fault burst (15:32 - 15:33)
        (0, 'kernel: INFO: task kubelet:5678 blocked for more than 120 seconds.'),
        (0.5, 'kernel: INFO: task mysqld:3456 blocked for more than 120 seconds.'),
        (1, 'kernel: INFO: task httpd:7890 blocked for more than 120 seconds.'),
        (1, 'kernel: INFO: rcu_sched detected stalls on CPUs/tasks: { 3} (detected by 0, t=6002 jiffies).'),
        (1.5, 'kernel: BUG: soft lockup - CPU#2 stuck for 23s! [kworker/2:1:456].'),
        (2, 'systemd: kubelet.service: Main process exited, code=killed, status=9/KILL.'),
        (2.5, 'systemd: mysqld.service: Main process exited, code=killed, status=9/KILL.'),
        (2.5, 'kernel: Out of memory: Kill process 3456 (mysqld) score 956 or sacrifice child.'),
        (3, 'kernel: Killed process 3456 (mysqld) total-vm:16777216kB, anon-rss:8388608kB, file-rss:1024kB.'),
        (3, 'systemd: httpd.service: Main process exited, code=killed, status=9/KILL.'),
        # Aftermath (15:34 - 15:35)
        (4, 'pacemakerd: notice: Node host1 state is now lost.'),
        (4.5, 'corosync: error: TOTEM: Retransmit List: 1 2 3 4 5.'),
        (5, 'kernel: possible SYN flooding on port 3306. Sending cookies.'),
        (5.5, 'kernel: nf_conntrack: table full, dropping packet.'),
        (6, 'kernel: net_ratelimit: 25 callbacks suppressed.'),
        # Recovery (15:37 - 15:38)
        (7, 'kernel: EXT4-fs (sda1): recovery complete.'),
        (8, 'kernel: EXT4-fs (sda1): mounted filesystem with ordered data mode.'),
        (10, 'systemd: Starting kubelet.service...'),
        (12, 'systemd: Starting mysqld.service...'),
        (14, 'systemd: Started kubelet.service.'),
    ]
    s1.dmesg_lines = [
        (-120, "EXT4-fs (sda1): mounted filesystem with ordered data mode."),
        (-30, "NET: Registered protocol family 10"),
        (-15, "nf_conntrack: default hash size 65536"),
        (-5, "INFO: task jbd2/sda1-8:1234 blocked for more than 120 seconds."),
        (-4, "Buffer I/O error on device sda1, logical block 123456."),
        (-3, "sd 0:0:0:0: [sda] Unhandled error code."),
        (-2, "end_request: I/O error, dev sda, sector 19126016."),
        (-1, "EXT4-fs error (device sda1): ext4_journal_start_sb: Detected aborted journal."),
        (0, "Remounting filesystem read-only."),
        (30, "INFO: task kubelet:5678 blocked for more than 120 seconds."),
        (60, "INFO: task mysqld:3456 blocked for more than 120 seconds."),
        (90, "INFO: rcu_sched detected stalls on CPUs/tasks: { 3}."),
        (120, "BUG: soft lockup - CPU#2 stuck for 23s! [kworker/2:1:456]."),
        (150, "Out of memory: Kill process 3456 (mysqld) score 956."),
        (180, "Killed process 3456 (mysqld) total-vm:16777216kB."),
        (300, "EXT4-fs (sda1): recovery complete."),
    ]
    scenarios.append(s1)

    # ================================================================
    # Scenario 2: Memory Exhaustion (Cgroup OOM)
    # Date: 2026-05-21, Fault: 10:00, Boot: 06:00
    # ================================================================
    s2 = Scenario(
        name="memory_exhaustion",
        date="2026-05-21",
        fault_hour=10, fault_minute=0,
        boot_hour=6, boot_minute=0,
        sar_spike={
            "memfree": MEMTOTAL_KB * 0.04,      # memused = 96% → MEMORY_PRESSURE HIGH (>90)
            "swapused": MEMTOTAL_KB * 0.80,     # >50% → SWAP_PRESSURE MEDIUM
            "cpu_iowait": 35.0,                 # avg (but max at 35) → won't trigger HIGH_IOWAIT
            "io_await": 300.0,                  # normal
            "io_util": 45.0,                    # normal
        },
    )
    s2.messages = [
        # Normal period
        (-15, "systemd: Starting user session."),
        (-12, "sshd[1234]: Accepted publickey for root from 10.0.1.50 port 22341."),
        (-10, "CRON[1500]: (root) CMD (/usr/lib/sa/sa1 1 1)"),
        (-8, "kernel: java invoked oom-killer: gfp_mask=0x201da, order=0, oom_score_adj=0."),
        (-6, "CRON[1520]: (root) CMD (run-parts /etc/cron.hourly)"),
        (-5, "kernel: Memory cgroup out of memory: Kill process 28901 (java) score 1024."),
        (-5, "kernel: Memory cgroup out of memory: Kill process 28902 (python) score 980."),
        (-4, "CRON[1540]: (root) CMD (logrotate /etc/logrotate.d/nginx)"),
        (-2, "kernel: task mysqld:8901 blocked for more than 120 seconds."),
        (-1, "kernel: INFO: task celery:12345 blocked for more than 120 seconds."),
        # Fault
        (0, "kernel: nginx invoked oom-killer: gfp_mask=0x24201ca, order=0, oom_score_adj=0."),
        (0.5, "kernel: Out of memory: Kill process 28901 (java) score 1024 or sacrifice child."),
        (1, "kernel: Killed process 28901 (java) total-vm:33554432kB, anon-rss:16777216kB, file-rss:2048kB."),
        (1.5, "kernel: Out of memory: Kill process 31200 (celery) score 890 or sacrifice child."),
        (2, "kernel: Killed process 31200 (celery) total-vm:8388608kB, anon-rss:4194304kB, file-rss:512kB."),
        (3, "systemd: nginx.service: Main process exited, code=killed, status=9/KILL."),
        # Aftermath
        (4, "kernel: Memory cgroup out of memory: Kill process 31500 (uwsgi) score 756."),
        (6, "kernel: task influxd:5678 blocked for more than 120 seconds."),
        (8, "systemd: uwsgi.service: Main process exited, code=killed, status=9/KILL."),
        (10, "systemd: Starting nginx.service..."),
        (12, "systemd: Started nginx.service."),
    ]
    s2.dmesg_lines = [
        (-600, "EXT4-fs (sda1): mounted filesystem with ordered data mode."),
        (-300, "systemd-journald[123]: Journal started."),
        (-120, "java invoked oom-killer: gfp_mask=0x201da, order=0."),
        (-60, "Memory cgroup out of memory: Kill process 28901 (java) score 1024."),
        (-30, "task mysqld:8901 blocked for more than 120 seconds."),
        (-10, "nginx invoked oom-killer: gfp_mask=0x24201ca, order=0."),
        (0, "Out of memory: Kill process 28901 (java) score 1024 or sacrifice child."),
        (10, "Killed process 28901 (java) total-vm:33554432kB."),
        (30, "Out of memory: Kill process 31200 (celery) score 890."),
        (60, "Killed process 31200 (celery) total-vm:8388608kB."),
        (120, "Memory cgroup out of memory: Kill process 31500 (uwsgi) score 756."),
    ]
    scenarios.append(s2)

    # ================================================================
    # Scenario 3: CPU Soft Lockup
    # Date: 2026-05-22, Fault: 14:30, Boot: 08:00
    # ================================================================
    s3 = Scenario(
        name="cpu_softlockup",
        date="2026-05-22",
        fault_hour=14, fault_minute=30,
        boot_hour=8, boot_minute=0,
        sar_spike={
            "cpu_steal": 40.0,          # >30 → CPU_STEAL HIGH
            "io_await": 6000.0,         # >5000 → IO_LATENCY CRITICAL
            "cpu_iowait": 25.0,         # under threshold, not triggered
            "io_util": 60.0,            # under threshold
            "memfree": MEMTOTAL_KB * 0.25,
            "swapused": MEMTOTAL_KB * 0.05,
        },
    )
    s3.messages = [
        # Normal period
        (-15, "systemd: Starting user session for root."),
        (-12, "sshd[3401]: Accepted publickey for root from 10.0.2.100 port 22341."),
        (-10, "CRON[3510]: (root) CMD (run-parts /etc/cron.hourly)"),
        (-8, "systemd: Removed slice User Slice of deploy."),
        (-6, "sshd[3560]: Accepted publickey for deploy from 10.0.2.101 port 33412."),
        # Precursor
        (-4, "kernel: INFO: task kworker/3:1:7890 blocked for more than 120 seconds."),
        (-3, "kernel: INFO: rcu_sched detected stalls on CPUs/tasks: { 3,5} (detected by 2, t=12005 jiffies)."),
        (-2, "kernel: INFO: task celery:23456 blocked for more than 120 seconds."),
        (-1, "kernel: BUG: soft lockup - CPU#3 stuck for 42s! [kworker/3:1:7890]."),
        # Fault burst (14:30 - 14:31)
        (0, "kernel: BUG: soft lockup - CPU#5 stuck for 67s! [celery:23456]."),
        (0.5, "kernel: INFO: rcu_sched detected stalls on CPUs/tasks: { 3,5,7} (detected by 1, t=24008 jiffies)."),
        (1, "kernel: INFO: task java:30001 blocked for more than 120 seconds."),
        (2, "kernel: BUG: soft lockup - CPU#7 stuck for 35s! [java:30001]."),
        (3, "kernel: hung_task: blocked tasks detected (java, celery, kworker)."),
        # Aftermath
        (5, "kernel: rcu_sched detected stalls on CPUs/tasks: { 3} (detected by 0, t=36002 jiffies)."),
        (8, "kernel: INFO: task httpd:45678 blocked for more than 120 seconds."),
        (12, "systemd: celery.service: state degraded."),
        (15, "CRON[4001]: (root) CMD (run-parts /etc/cron.hourly)"),
    ]
    s3.dmesg_lines = [
        (-900, "EXT4-fs (sda1): mounted filesystem with ordered data mode."),
        (-600, "systemd-journald[123]: Journal started."),
        (-240, "INFO: task kworker/3:1:7890 blocked for more than 120 seconds."),
        (-180, "INFO: rcu_sched detected stalls on CPUs/tasks: { 3,5}."),
        (-60, "BUG: soft lockup - CPU#3 stuck for 42s! [kworker/3:1:7890]."),
        (0, "BUG: soft lockup - CPU#5 stuck for 67s! [celery:23456]."),
        (30, "INFO: rcu_sched detected stalls on CPUs/tasks: { 3,5,7}."),
        (60, "BUG: soft lockup - CPU#7 stuck for 35s! [java:30001]."),
        (120, "hung_task: blocked tasks detected (java, celery, kworker)."),
        (300, "rcu_sched detected stalls on CPUs/tasks: { 3}."),
    ]
    scenarios.append(s3)

    # ================================================================
    # Scenario 4: Filesystem Corruption
    # Date: 2026-05-23, Fault: 22:00, Boot: 18:00
    # ================================================================
    s4 = Scenario(
        name="filesystem_corruption",
        date="2026-05-23",
        fault_hour=22, fault_minute=0,
        boot_hour=18, boot_minute=0,
        sar_spike={
            "io_util": 95.0,          # >90 → IO_SATURATION CRITICAL
            "io_await": 7000.0,       # >5000 → IO_LATENCY CRITICAL
            # avg await across all timestamps will be ~(7000*3 + 50*9)/12 ≈ 1787 > 1000 → IO_SLOW HIGH
            "cpu_iowait": 55.0,       # >50 → HIGH_IOWAIT HIGH (only at spike, not max>80)
            "memfree": MEMTOTAL_KB * 0.20,
            "swapused": MEMTOTAL_KB * 0.08,
        },
    )
    s4.messages = [
        # Normal period (21:45 - 21:57)
        (-15, "systemd: Starting user session for root."),
        (-12, "sshd[7801]: Accepted publickey for root from 192.168.1.100 port 55321."),
        (-10, "CRON[7900]: (root) CMD (run-parts /etc/cron.hourly)"),
        (-8, "systemd: Started Session 15 of user root."),
        (-6, "CRON[7950]: (root) CMD (/usr/lib/sa/sa1 1 1)"),
        # Precursor (21:58 - 21:59)
        (-4, "kernel: Buffer I/O error on device sdb1, logical block 987654."),
        (-3.5, "kernel: sd 1:0:0:0: [sdb] Unhandled error code."),
        (-3, "kernel: sd 1:0:0:0: [sdb] Result: hostbyte=DID_ERROR driverbyte=DRIVER_OK."),
        (-2.5, "kernel: end_request: I/O error, dev sdb, sector 82345678."),
        (-2, "kernel: EXT4-fs error (device sdb1): ext4_journal_start_sb: Detected aborted journal."),
        (-1.5, "kernel: journal commit I/O error for device sdb1."),
        (-1, "kernel: Buffer I/O error on device sdb1, logical block 987655."),
        (-0.5, "kernel: EXT4-fs error (device sdb1): ext4_find_entry: reading directory #123456."),
        # Fault (22:00)
        (0, "kernel: Remounting filesystem read-only."),
        (1, "kernel: EXT4-fs error (device sdb1): ext4_writepage: IO failure writing to device."),
        (1.5, "kernel: Read-only file system"),
        (2, "kernel: INFO: task postgres:45000 blocked for more than 120 seconds."),
        (3, "kernel: INFO: task redis:45100 blocked for more than 120 seconds."),
        (3, "kernel: journal commit I/O error for device sdb1."),
        # Aftermath
        (5, "kernel: Buffer I/O error on device sdb1, logical block 987656."),
        (6, "kernel: end_request: I/O error, dev sdb, sector 82345679."),
        (8, "kernel: EXT4-fs error (device sdb1): ext4_readdir: bad directory entry."),
        (10, "systemd: postgres.service: Main process exited, code=killed, status=9/KILL."),
        (15, "kernel: EXT4-fs (sdb1): recovery complete."),
    ]
    s4.dmesg_lines = [
        (-240, "EXT4-fs (sdb1): mounted filesystem with ordered data mode."),
        (-120, "systemd-journald[123]: Journal started."),
        (-60, "Buffer I/O error on device sdb1, logical block 987654."),
        (-30, "sd 1:0:0:0: [sdb] Unhandled error code."),
        (-20, "end_request: I/O error, dev sdb, sector 82345678."),
        (-10, "EXT4-fs error (device sdb1): ext4_journal_start_sb: Detected aborted journal."),
        (-5, "journal commit I/O error for device sdb1."),
        (0, "Remounting filesystem read-only."),
        (30, "EXT4-fs error (device sdb1): ext4_writepage: IO failure writing to device."),
        (60, "Read-only file system"),
        (120, "INFO: task postgres:45000 blocked for more than 120 seconds."),
        (300, "journal commit I/O error for device sdb1."),
        (600, "EXT4-fs (sdb1): recovery complete."),
    ]
    scenarios.append(s4)

    return scenarios


# ============================================================
# Main
# ============================================================

def main():
    output_dir = Path(__file__).parent.parent / "test_scenarios"
    print(f"Generating test scenarios → {output_dir}/")
    print()

    for s in define_scenarios():
        print(f"Scenario: {s.name}")
        print(f"  Date: {s.date}, Fault: {s.fault_hour:02d}:{s.fault_minute:02d}, Boot: {s.boot_hour:02d}:{s.boot_minute:02d}")
        print(f"  Messages: {len(s.messages)} lines, Dmesg: {len(s.dmesg_lines)} lines, SAR spike: {list(s.sar_spike.keys())}")
        build_tar(s, output_dir)
        print()

    print("Done. All 4 test scenarios generated.")


if __name__ == "__main__":
    main()
