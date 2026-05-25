"""生成最小 mock sosreport 用于开发测试。

模拟一个遭遇 IO hang 故障的场景：
- 故障时间 15:32
- 日志包含 iowait 升高、blocked task、load 飙升等关键事件
"""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).parent.parent / "sample_data" / "mock_sosreport"


def create_mock_sosreport():
    if ROOT.exists():
        shutil.rmtree(ROOT)

    # --- var/log/messages ---
    messages_dir = ROOT / "var" / "log"
    messages_dir.mkdir(parents=True)

    messages = [
        # 正常时段 (15:20-15:29)
        'Jul 18 15:20:01 host1 systemd: Starting Session 42 of user root.',
        'Jul 18 15:21:15 host1 kernel: EXT4-fs (sda1): mounted filesystem with ordered data mode.',
        'Jul 18 15:22:30 host1 sshd[2841]: Accepted publickey for root from 10.0.1.100 port 52341.',
        'Jul 18 15:23:45 host1 CRON[2892]: (root) CMD (run-parts /etc/cron.hourly)',
        'Jul 18 15:24:00 host1 systemd: Started Session 43 of user root.',
        'Jul 18 15:25:10 host1 kernel: nf_conntrack: default hash size 65536.',
        'Jul 18 15:26:22 host1 sshd[2901]: Accepted publickey for root from 10.0.1.101 port 52342.',
        'Jul 18 15:27:30 host1 CRON[2910]: (root) CMD (/usr/lib/sa/sa1 1 1)',
        'Jul 18 15:28:15 host1 systemd: Removed slice User Slice of root.',
        'Jul 18 15:29:00 host1 rsyslogd: [origin software="rsyslogd"] restart.',
        # 故障前兆 (15:30-15:31)
        'Jul 18 15:30:25 host1 kernel: INFO: task jbd2/sda1-8:1234 blocked for more than 120 seconds.',
        'Jul 18 15:30:26 host1 kernel:       Tainted: G          ------------ T 2.6.32-754.el6.x86_64 #1',
        'Jul 18 15:30:26 host1 kernel: "echo 0 > /proc/sys/kernel/hung_task_timeout_secs" disables this message.',
        'Jul 18 15:30:30 host1 kernel: Buffer I/O error on device sda1, logical block 123456.',
        'Jul 18 15:30:35 host1 multipathd: sda: add missing path.',
        'Jul 18 15:30:40 host1 kernel: sd 0:0:0:0: [sda] Unhandled error code.',
        'Jul 18 15:30:45 host1 kernel: sd 0:0:0:0: [sda] Result: hostbyte=DID_ERROR driverbyte=DRIVER_OK.',
        'Jul 18 15:30:50 host1 kernel: sd 0:0:0:0: [sda] CDB: Write(10) 2a 00 01 23 45 00 00 08 00.',
        'Jul 18 15:31:00 host1 kernel: end_request: I/O error, dev sda, sector 19126016.',
        'Jul 18 15:31:10 host1 kernel: EXT4-fs error (device sda1): ext4_journal_start_sb: Detected aborted journal.',
        'Jul 18 15:31:15 host1 kernel: Remounting filesystem read-only.',
        # 故障爆发 (15:32-15:33)
        'Jul 18 15:32:01 host1 kernel: INFO: task kubelet:5678 blocked for more than 120 seconds.',
        'Jul 18 15:32:02 host1 kernel: INFO: task mysqld:3456 blocked for more than 120 seconds.',
        'Jul 18 15:32:05 host1 kernel: INFO: task httpd:7890 blocked for more than 120 seconds.',
        'Jul 18 15:32:10 host1 kernel: INFO: rcu_sched detected stalls on CPUs/tasks: { 3} (detected by 0, t=6002 jiffies).',
        'Jul 18 15:32:15 host1 kernel: BUG: soft lockup - CPU#2 stuck for 23s! [kworker/2:1:456].',
        'Jul 18 15:32:20 host1 systemd: kubelet.service: Main process exited, code=killed, status=9/KILL.',
        'Jul 18 15:32:25 host1 systemd: mysqld.service: Main process exited, code=killed, status=9/KILL.',
        'Jul 18 15:32:30 host1 kernel: Out of memory: Kill process 3456 (mysqld) score 956 or sacrifice child.',
        'Jul 18 15:32:31 host1 kernel: Killed process 3456 (mysqld) total-vm:16777216kB, anon-rss:8388608kB, file-rss:1024kB.',
        'Jul 18 15:32:35 host1 systemd: httpd.service: Main process exited, code=killed, status=9/KILL.',
        # 持续影响 (15:33-15:35)
        'Jul 18 15:33:00 host1 kernel: INFO: task jbd2/sda1-8:1234 blocked for more than 240 seconds.',
        'Jul 18 15:33:30 host1 pacemakerd: notice: Node host1 state is now lost.',
        'Jul 18 15:34:00 host1 corosync: error: TOTEM: Retransmit List: 1 2 3 4 5.',
        'Jul 18 15:34:30 host1 kernel: net_ratelimit: 25 callbacks suppressed.',
        'Jul 18 15:34:50 host1 kernel: possible SYN flooding on port 3306. Sending cookies.',
        'Jul 18 15:35:00 host1 kernel: nf_conntrack: table full, dropping packet.',
        # 恢复尝试
        'Jul 18 15:35:30 host1 kernel: EXT4-fs (sda1): recovery complete.',
        'Jul 18 15:36:00 host1 kernel: EXT4-fs (sda1): mounted filesystem with ordered data mode.',
        'Jul 18 15:37:00 host1 systemd: Starting kubelet.service...',
        'Jul 18 15:38:00 host1 systemd: Starting mysqld.service...',
    ]

    (messages_dir / "messages").write_text("\n".join(messages) + "\n")

    # --- sos_commands/kernel/dmesg ---
    dmesg_dir = ROOT / "sos_commands" / "kernel"
    dmesg_dir.mkdir(parents=True)

    dmesg_lines = [
        "[    0.000000] Initializing cgroup subsys cpuset",
        "[    0.000000] Linux version 2.6.32-754.el6.x86_64 (mockbuild@x86-01.bsys.centos.org)",
        "[    0.000000] Command line: ro root=/dev/mapper/vg-root rd_NO_LUKS rd_LVM_LV=vg/root",
        "[    0.000000] KERNEL supported cpus:",
        "[    1.234567] EXT4-fs (sda1): mounted filesystem with ordered data mode.",
        "[   10.123456] NET: Registered protocol family 10",
        "[12345.678901] INFO: task jbd2/sda1-8:1234 blocked for more than 120 seconds.",
        "[12346.123456] Buffer I/O error on device sda1, logical block 123456.",
        "[12360.000000] EXT4-fs error (device sda1): ext4_journal_start_sb: Detected aborted journal.",
        "[12361.500000] Remounting filesystem read-only.",
        "[12480.000000] INFO: task kubelet:5678 blocked for more than 120 seconds.",
        "[12480.100000] INFO: task mysqld:3456 blocked for more than 120 seconds.",
        "[12490.000000] INFO: rcu_sched detected stalls on CPUs/tasks: { 3}.",
        "[12500.000000] BUG: soft lockup - CPU#2 stuck for 23s!",
        "[12510.000000] Out of memory: Kill process 3456 (mysqld) score 956.",
        "[12520.000000] Killed process 3456 (mysqld).",
        "[12780.000000] EXT4-fs (sda1): recovery complete.",
    ]

    (dmesg_dir / "dmesg").write_text("\n".join(dmesg_lines) + "\n")

    # --- proc/stat (含 btime) ---
    # btime = 1711300000 → 对应 2024-03-24 某个时间点
    # 故障时间 15:32 → dmesg 中 blocked task 在 uptime 12345.678 秒
    # boot_time + 12345 = 15:32 的时间戳
    proc_dir = ROOT / "proc"
    proc_dir.mkdir(parents=True)

    boot_time = 1711300000  # 简化: 假设这就是 boot_time
    (proc_dir / "stat").write_text(
        f"cpu  1123456 12345 987654 98765432 12345 0 1234 0 0 0\n"
        f"cpu0 561728 6172 493827 49382716 6172 0 617 0 0 0\n"
        f"cpu1 561728 6173 493827 49382716 6173 0 617 0 0 0\n"
        f"intr 1234567890 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
        f"ctxt 9876543210\n"
        f"btime {boot_time}\n"
        f"processes 123456\n"
        f"procs_running 5\n"
        f"procs_blocked 12\n"
        f"softirq 1234567890 0 0 0 0 0 0 0 0 0 0 0\n"
    )

    # --- proc/meminfo ---
    (proc_dir / "meminfo").write_text(
        "MemTotal:       16777216 kB\n"
        "MemFree:          524288 kB\n"
        "MemAvailable:     262144 kB\n"
        "Buffers:          131072 kB\n"
        "Cached:          2097152 kB\n"
        "SwapCached:       524288 kB\n"
        "SwapTotal:       8388608 kB\n"
        "SwapFree:        2097152 kB\n"
    )

    # --- proc/loadavg ---
    (proc_dir / "loadavg").write_text("88.20 75.30 45.10 5/1024 12345\n")

    # --- etc/fstab ---
    etc_dir = ROOT / "etc"
    etc_dir.mkdir(parents=True)
    (etc_dir / "fstab").write_text(
        "/dev/mapper/vg-root / ext4 defaults 1 1\n"
        "/dev/sda1 /boot ext4 defaults 1 2\n"
        "/dev/mapper/vg-data /data ext4 defaults 0 0\n"
    )

    print(f"Mock sosreport created at: {ROOT}")
    print(f"  var/log/messages:      {len(messages)} lines")
    print(f"  sos_commands/kernel/dmesg: {len(dmesg_lines)} lines")
    print(f"  proc/stat:             boot_time={boot_time}")
    print(f"  proc/meminfo + proc/loadavg + etc/fstab")


if __name__ == "__main__":
    create_mock_sosreport()
