"""在 VM 上生成真实测试场景的自动化脚本。

每个场景：
1. SSH 到 VM，重启（清空 dmesg）
2. 等待系统就绪
3. 手动采集 SAR 基线数据
4. 运行 stress-ng + 注入内核异常
5. 采集 SAR 故障数据
6. 运行 sosreport
7. 用 sadf -x 把二进制 SA 转成 XML
8. 把 XML 放入 sos_commands/sar/ 目录
9. 重新打包并下载到 Windows
"""
import io
import os
import paramiko
import tarfile
import time
import re
from pathlib import Path

VM_HOST = "192.168.234.128"
VM_USER = "ity"
VM_PASSWORD = "5420"
SUDO_PREFIX = f"echo {VM_PASSWORD} | sudo -S "
TEST_DIR = Path(__file__).parent.parent / "test_scenarios"


def ssh_connect():
    """连接 VM，返回 SSHClient。"""
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(VM_HOST, username=VM_USER, password=VM_PASSWORD, timeout=15)
    return c


def ssh_exec(cmd: str, timeout: int = 30):
    """执行命令（非 root），返回 stdout 字符串。"""
    c = ssh_connect()
    try:
        stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
        return stdout.read().decode()
    finally:
        c.close()


def ssh_sudo(cmd: str, timeout: int = 30):
    """执行 sudo 命令，返回 stdout 字符串。"""
    return ssh_exec(f"{SUDO_PREFIX} {cmd}", timeout)


def ssh_exec_live(c, cmd: str, timeout: int = 30):
    """在已有连接上执行命令。"""
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    return stdout.read().decode(), stderr.read().decode()


def ssh_sudo_live(c, cmd: str, timeout: int = 30):
    """在已有连接上执行 sudo 命令。"""
    return ssh_exec_live(c, f"{SUDO_PREFIX} {cmd}", timeout)


def wait_for_vm(timeout: int = 120):
    """等待 VM 重启后恢复 SSH 连接。"""
    print(f"  等待 VM 重启（最多 {timeout}s）...", end="", flush=True)
    for i in range(timeout, 0, -5):
        time.sleep(5)
        try:
            c = ssh_connect()
            c.close()
            print(" 就绪！")
            return True
        except Exception:
            print(f"\r  等待 VM 重启（最多 {timeout}s）... 还剩 {i-5}s", end="", flush=True)
    print(" 超时！")
    return False


def collect_sar_snapshot(c, label: str):
    """手动触发一次 SAR 数据采集（sadc 1 1 追加到当天 sa 文件）。"""
    cmd = f"{SUDO_PREFIX} /usr/lib/sysstat/sadc 1 1 /var/log/sa/sa$(date +%d)"
    stdout, stderr = ssh_exec_live(c, cmd)
    print(f"  SAR snapshot [{label}]: OK")


def run_scenario(scenario_name: str, boot_time_str: str,
                 stress_cmd: str, kmsg_lines: list[str]):
    """在 VM 上生成一个测试场景。

    Args:
        scenario_name: 场景名称（用于输出文件名）
        boot_time_str: 故障时间，如 "15:32"（用于 sosreport 时间窗口对齐）
        stress_cmd: stress-ng 命令
        kmsg_lines: 要写入 /dev/kmsg 的异常消息列表
    """
    print(f"\n{'='*60}")
    print(f"场景: {scenario_name}")
    print(f"{'='*60}")

    # Step 1: 重启 VM 清空 dmesg
    print(f"\n[1/7] 重启 VM...")
    ssh_sudo("reboot", timeout=5)
    time.sleep(5)

    if not wait_for_vm():
        print("ERROR: VM 重启后无法连接！")
        return None

    c = ssh_connect()

    # Step 2: 等待系统稳定 + 采集 SAR 基线
    print("\n[2/7] 采集 SAR 基线数据...")
    time.sleep(30)  # 等系统稳定
    for i in range(3):
        collect_sar_snapshot(c, f"baseline-{i+1}")
        time.sleep(60)  # 间隔 1 分钟

    # Step 3: 运行 stress-ng + 注入异常
    print(f"\n[3/7] 运行 stress-ng 并注入内核异常...")
    # 启动 stress-ng（后台运行）
    ssh_exec_live(c, f"nohup {stress_cmd} > /tmp/stress.log 2>&1 &")
    time.sleep(5)  # 等 stress-ng 开始施压

    # 写入 /dev/kmsg 异常消息
    for msg in kmsg_lines:
        escaped = msg.replace('"', '\\"').replace('!', '\\!')
        cmd = f"{SUDO_PREFIX} bash -c 'echo \"{escaped}\" >> /dev/kmsg'"
        ssh_exec_live(c, cmd)
        time.sleep(1)

    # Step 4: 采集 SAR 故障数据
    print(f"\n[4/7] 采集 SAR 故障期数据...")
    for i in range(3):
        collect_sar_snapshot(c, f"fault-{i+1}")
        time.sleep(60)

    # 等 stress-ng 完成（如果还在跑）
    time.sleep(30)

    # 再采集几点恢复期数据
    for i in range(2):
        collect_sar_snapshot(c, f"recovery-{i+1}")
        time.sleep(60)

    # Step 5: 运行 sosreport
    print(f"\n[5/7] 运行 sosreport...")
    stdout, stderr = ssh_sudo_live(c, "sosreport --batch --tmp-dir /tmp 2>&1", timeout=120)
    print(stdout)

    # 找到生成的 sosreport 路径
    tar_path = None
    for line in stdout.split("\n"):
        # 典型输出: "Your sosreport has been generated in /tmp/sosreport-xxx.tar.xz"
        if ".tar" in line.lower() and "/" in line:
            m = re.search(r"(/\S+\.tar\.\w+)", line)
            if m:
                tar_path = m.group(1)
                break

    if not tar_path:
        # 尝试找最新的 tar 文件
        stdout2, _ = ssh_exec_live(c, "ls -t /tmp/sosreport-*.tar.* 2>/dev/null | head -1")
        tar_path = stdout2.strip()

    if not tar_path:
        print("ERROR: 找不到 sosreport tar 文件！")
        c.close()
        return None

    print(f"  SOSReport: {tar_path}")

    # Step 6: 解压、转换 SAR、重新打包
    print(f"\n[6/7] 转换二进制 SAR → XML...")

    extract_dir = f"/tmp/{scenario_name}_extract"
    convert_script = f"""#!/bin/bash
set -e
TARBALL="{tar_path}"
EXTRACT="{extract_dir}"
rm -rf "$EXTRACT"
mkdir -p "$EXTRACT"
cd "$EXTRACT"
tar -xf "$TARBALL" 2>/dev/null
SOSDIR=$(ls -d */ | head -1)
echo "SOSDIR: $SOSDIR"

# Convert binary SA to XML
SA_DIR="$EXTRACT/$SOSDIR/var/log/sa"
XML_DIR="$EXTRACT/$SOSDIR/sos_commands/sar"
mkdir -p "$XML_DIR"

if ls "$SA_DIR"/sa[0-9]* 2>/dev/null; then
    for sa in "$SA_DIR"/sa[0-9]*; do
        bn=$(basename "$sa")
        sadf -x "$sa" -- -A > "$XML_DIR/$bn.xml" 2>/dev/null && echo "  $bn -> $bn.xml" || echo "  $bn FAILED"
    done
fi

# Re-tar with xz
OUTNAME="{scenario_name}.tar.xz"
cd "$EXTRACT"
tar -cJf "/tmp/$OUTNAME" "$SOSDIR"
echo "DONE: /tmp/$OUTNAME"
ls -lh "/tmp/$OUTNAME"
"""
    # Write convert script to VM
    sftp = c.open_sftp()
    with sftp.open("/tmp/convert.sh", "w") as f:
        f.write(convert_script)
    sftp.close()

    stdout, stderr = ssh_sudo_live(c, "bash /tmp/convert.sh 2>&1", timeout=120)
    print(stdout)
    if stderr:
        print("STDERR:", stderr[:500])

    # Step 7: 下载到 Windows
    print(f"\n[7/7] 下载到 test_scenarios/...")
    remote_path = f"/tmp/{scenario_name}.tar.xz"
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    local_path = TEST_DIR / f"{scenario_name}.tar.xz"

    sftp = c.open_sftp()
    sftp.get(remote_path, str(local_path))
    sftp.close()

    size_kb = local_path.stat().st_size / 1024
    print(f"  下载完成: {local_path} ({size_kb:.1f} KB)")

    c.close()
    return local_path


# ================================================================
# 场景定义
# ================================================================

SCENARIOS = [
    {
        "name": "io_hang_oom_real",
        "boot_time_str": "",
        "stress_cmd": "stress-ng --hdd 4 --io 4 --timeout 180s",
        "kmsg_lines": [
            "INFO: task jbd2/sda1-8:1234 blocked for more than 120 seconds.",
            "Buffer I/O error on device sda1, logical block 123456.",
            "sd 0:0:0:0: [sda] Unhandled error code.",
            "sd 0:0:0:0: [sda] Result: hostbyte=DID_ERROR driverbyte=DRIVER_OK.",
            "end_request: I/O error, dev sda, sector 19126016.",
            "EXT4-fs error (device sda1): ext4_journal_start_sb: Detected aborted journal.",
            "Remounting filesystem read-only.",
            "INFO: task kubelet:5678 blocked for more than 120 seconds.",
            "INFO: task mysqld:3456 blocked for more than 120 seconds.",
            "INFO: rcu_sched detected stalls on CPUs/tasks: { 3} (detected by 0, t=6002 jiffies).",
            "BUG: soft lockup - CPU#2 stuck for 23s! [kworker/2:1:456].",
            "Out of memory: Kill process 3456 (mysqld) score 956 or sacrifice child.",
            "Killed process 3456 (mysqld) total-vm:16777216kB, anon-rss:8388608kB.",
            "possible SYN flooding on port 3306. Sending cookies.",
            "nf_conntrack: table full, dropping packet.",
        ],
    },
    {
        "name": "memory_exhaustion_real",
        "boot_time_str": "",
        "stress_cmd": "stress-ng --vm 2 --vm-bytes 90% --timeout 180s",
        "kmsg_lines": [
            "java invoked oom-killer: gfp_mask=0x201da, order=0, oom_score_adj=0.",
            "Memory cgroup out of memory: Kill process 28901 (java) score 1024.",
            "Memory cgroup out of memory: Kill process 28902 (python) score 980.",
            "task mysqld:8901 blocked for more than 120 seconds.",
            "INFO: task celery:12345 blocked for more than 120 seconds.",
            "nginx invoked oom-killer: gfp_mask=0x24201ca, order=0, oom_score_adj=0.",
            "Out of memory: Kill process 28901 (java) score 1024 or sacrifice child.",
            "Killed process 28901 (java) total-vm:33554432kB, anon-rss:16777216kB.",
            "Out of memory: Kill process 31200 (celery) score 890 or sacrifice child.",
            "Killed process 31200 (celery) total-vm:8388608kB.",
            "Memory cgroup out of memory: Kill process 31500 (uwsgi) score 756.",
        ],
    },
    {
        "name": "cpu_softlockup_real",
        "boot_time_str": "",
        "stress_cmd": "stress-ng --cpu 8 --timeout 180s",
        "kmsg_lines": [
            "INFO: task kworker/3:1:7890 blocked for more than 120 seconds.",
            "INFO: rcu_sched detected stalls on CPUs/tasks: { 3,5} (detected by 2, t=12005 jiffies).",
            "INFO: task celery:23456 blocked for more than 120 seconds.",
            "BUG: soft lockup - CPU#3 stuck for 42s! [kworker/3:1:7890].",
            "BUG: soft lockup - CPU#5 stuck for 67s! [celery:23456].",
            "INFO: rcu_sched detected stalls on CPUs/tasks: { 3,5,7} (detected by 1, t=24008 jiffies).",
            "BUG: soft lockup - CPU#7 stuck for 35s! [java:30001].",
            "hung_task: blocked tasks detected (java, celery, kworker).",
        ],
    },
    {
        "name": "filesystem_corruption_real",
        "boot_time_str": "",
        "stress_cmd": "stress-ng --hdd 4 --io 4 --timeout 180s",
        "kmsg_lines": [
            "Buffer I/O error on device sdb1, logical block 987654.",
            "sd 1:0:0:0: [sdb] Unhandled error code.",
            "sd 1:0:0:0: [sdb] Result: hostbyte=DID_ERROR driverbyte=DRIVER_OK.",
            "end_request: I/O error, dev sdb, sector 82345678.",
            "EXT4-fs error (device sdb1): ext4_journal_start_sb: Detected aborted journal.",
            "journal commit I/O error for device sdb1.",
            "Buffer I/O error on device sdb1, logical block 987655.",
            "EXT4-fs error (device sdb1): ext4_find_entry: reading directory #123456.",
            "Remounting filesystem read-only.",
            "EXT4-fs error (device sdb1): ext4_writepage: IO failure writing to device.",
            "Read-only file system",
            "INFO: task postgres:45000 blocked for more than 120 seconds.",
            "journal commit I/O error for device sdb1.",
        ],
    },
]


def main():
    print("VM 真实测试场景生成器")
    print(f"VM: {VM_HOST}")
    print(f"输出目录: {TEST_DIR}")
    print()

    # 先验证 VM 连接
    print("验证 VM 连接...")
    try:
        c = ssh_connect()
        stdout, _ = ssh_exec_live(c, "uptime && echo '---' && ls /var/log/sa/ 2>&1")
        print(f"VM uptime: {stdout.strip()}")
        c.close()
    except Exception as e:
        print(f"ERROR: 无法连接 VM: {e}")
        return

    results = []
    for i, sc in enumerate(SCENARIOS):
        print(f"\n{'#'*60}")
        print(f"# 场景 {i+1}/{len(SCENARIOS)}: {sc['name']}")
        print(f"{'#'*60}")
        try:
            result = run_scenario(
                scenario_name=sc["name"],
                boot_time_str=sc.get("boot_time_str", ""),
                stress_cmd=sc["stress_cmd"],
                kmsg_lines=sc["kmsg_lines"],
            )
            results.append((sc["name"], result))
        except Exception as e:
            print(f"ERROR running {sc['name']}: {e}")
            import traceback
            traceback.print_exc()
            results.append((sc["name"], None))

    # Summary
    print(f"\n{'='*60}")
    print("生成结果汇总")
    print(f"{'='*60}")
    for name, path in results:
        if path:
            size_kb = Path(path).stat().st_size / 1024
            print(f"  {name}: {path} ({size_kb:.1f} KB)")
        else:
            print(f"  {name}: FAILED")


if __name__ == "__main__":
    main()
