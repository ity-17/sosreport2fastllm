#!/usr/bin/env python3
"""在 VM 上运行：将 sadf -j JSON 输出转换为解析器兼容的 XML 格式。

用法: python3 sadf_json_to_xml.py <sa_file_path> <output_xml_path>
或: sadf -j /var/log/sa/sa25 -- -A | python3 sadf_json_to_xml.py - output.xml

sadf JSON key → XML attribute 映射:
  CPU: usr→user, sys→system, soft→softirq, cpu→number
  Memory: 直接映射（memfree, memused, swapused 等）
  Disk: disk-device→device
  Network: 直接映射
"""
import json
import sys
from xml.etree import ElementTree as ET
from datetime import datetime

# sadf JSON → 解析器 XML 的 CPU key 映射
CPU_KEY_MAP = {
    "usr": "user",
    "sys": "system",
    "soft": "softirq",
    "iowait": "iowait",
    "steal": "steal",
    "idle": "idle",
    "irq": "irq",
    "nice": "nice",
}

MEM_KEYS = ("memfree", "memused", "memused-percent", "swapfree", "swapused",
            "buffers", "cached", "commit", "commit-percent", "active", "inactive")

# sadf JSON uses abbreviated names for swap fields
MEM_KEY_MAP = {
    "memfree": "memfree", "memused": "memused",
    "memused-percent": "memused-percent",
    "swpfree": "swapfree", "swpused": "swapused",
    "buffers": "buffers", "cached": "cached",
    "commit": "commit", "commit-percent": "commit-percent",
    "active": "active", "inactive": "inactive",
}

DISK_KEYS = ("await", "util", "svctm", "avgqu-sz", "avgrq-sz",
             "r_await", "w_await", "rkB", "wkB", "tps")

NET_KEYS = ("rxdrop", "txdrop", "rxerr", "txerr", "rxpck", "txpck", "rxkB", "txkB")


def convert(json_input: str) -> str:
    """将 sadf -j 的 JSON 输出转换为解析器兼容的 SAR XML 字符串。"""
    data = json.loads(json_input)

    ns = "http://pagesperso-orange.fr/sebastien.godard/sysstat"
    ET.register_namespace("", ns)
    root = ET.Element(f"{{{ns}}}sysstat")

    hostname = "unknown"
    statistics = []

    for host_entry in data.get("sysstat", {}).get("hosts", []):
        hostname = host_entry.get("nodename", hostname)
        statistics.extend(host_entry.get("statistics", []))

    # Host element
    host_elem = ET.SubElement(root, f"{{{ns}}}host")
    host_elem.set("nodename", hostname)

    for stat in statistics:
        ts_info = stat.get("timestamp", {})
        date_str = ts_info.get("date", "")
        time_str = ts_info.get("time", "")
        if not date_str or not time_str:
            # Fallback: try epoch field (some sadf versions use this)
            epoch_ts = ts_info.get("epoch", 0)
            if not epoch_ts:
                continue
            dt = datetime.fromtimestamp(epoch_ts)
            date_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H:%M:%S")

        ts_elem = ET.SubElement(root, f"{{{ns}}}timestamp")
        ts_elem.set("date", date_str)
        ts_elem.set("time", time_str)
        ts_elem.set("utc", "0")  # sadf date/time is local time, tell parser not to add offset

        # CPU: sadf uses usr/sys/soft → parser expects user/system/softirq
        cpu_list = stat.get("cpu-load", [])
        if cpu_list:
            cpu_load = ET.SubElement(ts_elem, f"{{{ns}}}cpu-load")
            for cpu_data in cpu_list:
                cpu = ET.SubElement(cpu_load, f"{{{ns}}}cpu")
                cpu.set("number", str(cpu_data.get("cpu", "all")))
                for json_key, xml_key in CPU_KEY_MAP.items():
                    val = cpu_data.get(json_key)
                    if val is not None:
                        cpu.set(xml_key, f"{float(val):.2f}")

        # Memory (sadf uses swpfree/swpused → parser expects swapfree/swapused)
        mem_data = stat.get("memory", {})
        if mem_data:
            mem = ET.SubElement(ts_elem, f"{{{ns}}}memory")
            for json_key, xml_key in MEM_KEY_MAP.items():
                val = mem_data.get(json_key)
                if val is not None:
                    mem.set(xml_key, f"{float(val):.2f}")

        # Disk (extended stats: await, util, etc.)
        # Note: sadf -j uses "disk" key (not "io") for extended stats
        disk_list = stat.get("disk", [])
        if not disk_list:
            # Fallback: try to extract basic IO from "io" section
            io_data = stat.get("io", {})
            if io_data:
                disk = ET.SubElement(ts_elem, f"{{{ns}}}disk")
                disk.set("device", "all")
                tps = io_data.get("tps")
                if tps is not None:
                    disk.set("tps", f"{float(tps):.2f}")

        for disk_data in disk_list:
            disk = ET.SubElement(ts_elem, f"{{{ns}}}disk")
            disk.set("device", disk_data.get("disk-device", disk_data.get("device", "unknown")))
            for k in DISK_KEYS:
                val = disk_data.get(k)
                if val is not None:
                    disk.set(k, f"{float(val):.2f}")

        # Network
        net_data = stat.get("network", {})
        net_dev_list = net_data.get("net-dev", [])
        for nd in net_dev_list:
            net_dev = ET.SubElement(ts_elem, f"{{{ns}}}net-dev")
            net_dev.set("iface", nd.get("iface", ""))
            for k in NET_KEYS:
                val = nd.get(k)
                if val is not None:
                    net_dev.set(k, f"{float(val):.2f}")

        # Load average (from queue element in sadf -j)
        queue = stat.get("queue", {})
        if queue:
            la = ET.SubElement(ts_elem, f"{{{ns}}}load-average")
            ldavg_map = {"load1": "ldavg-1", "load5": "ldavg-5", "load15": "ldavg-15"}
            for xml_key, json_key in ldavg_map.items():
                val = queue.get(json_key)
                if val is not None:
                    la.set(xml_key, f"{float(val):.2f}")

    return ET.tostring(root, encoding="unicode")


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        with open(sys.argv[1]) as f:
            json_str = f.read()
    else:
        json_str = sys.stdin.read()

    xml_output = convert(json_str)

    if len(sys.argv) >= 3:
        with open(sys.argv[2], "w") as f:
            f.write(xml_output)
        print(f"Written to {sys.argv[2]}")
    else:
        print(xml_output)
