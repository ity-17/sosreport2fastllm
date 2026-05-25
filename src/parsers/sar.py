"""SAR parser: XML first -> binary sadf fallback.

Yields (timestamp, metric, value, host) tuples directly to avoid
materializing a 260K-entry list in memory.
"""
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from typing import Iterator


def _parse_sar_xml_file(xml_path: Path) -> Iterator[tuple[int, str, float, str]]:
    """Generator: yield (timestamp, metric, value, host) from a SAR XML file."""
    try:
        tree = ET.parse(str(xml_path))
        root = tree.getroot()
    except (ET.ParseError, IOError):
        return

    ns = root.tag.split("}")[0] + "}" if "}" in root.tag else ""

    host_elem = root.find(f".//{ns}host")
    hostname = host_elem.get("nodename", "") if host_elem is not None else ""

    for ts_elem in root.iter(f"{ns}timestamp"):
        date_str = ts_elem.get("date", "")
        time_str = ts_elem.get("time", "")

        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
            ts = int(dt.timestamp())
        except ValueError:
            continue

        if ts_elem.get("utc", "0") == "1":
            ts += 8 * 3600

        # CPU load
        for cpu_load in ts_elem.iter(f"{ns}cpu-load"):
            for cpu in cpu_load.iter(f"{ns}cpu"):
                for key in ("user", "nice", "system", "iowait", "steal", "idle", "softirq", "irq"):
                    val = cpu.get(key)
                    if val is not None:
                        try:
                            yield (ts, f"cpu_{key}", float(val), hostname)
                        except ValueError:
                            pass

        # Memory
        for mem in ts_elem.iter(f"{ns}memory"):
            for key in ("memfree", "memused", "memused-percent", "swapfree", "swapused",
                        "buffers", "cached", "commit", "commit-percent", "active", "inactive"):
                val = mem.get(key)
                if val is not None:
                    try:
                        yield (ts, f"mem_{key.replace('-', '_')}", float(val), hostname)
                    except ValueError:
                        pass

        # Disk I/O
        for disk in ts_elem.iter(f"{ns}disk"):
            for key in ("await", "util", "svctm", "avgqu-sz", "avgrq-sz",
                        "r_await", "w_await", "rkB", "wkB", "tps"):
                val = disk.get(key)
                if val is not None:
                    try:
                        yield (ts, f"io_{key}", float(val), hostname)
                    except ValueError:
                        pass

        # Network
        for net_dev in ts_elem.iter(f"{ns}net-dev"):
            iface = net_dev.get("iface", "")
            for key in ("rxdrop", "txdrop", "rxerr", "txerr", "rxpck", "txpck", "rxkB", "txkB"):
                val = net_dev.get(key)
                if val is not None:
                    try:
                        yield (ts, f"net_{iface}_{key}", float(val), hostname)
                    except ValueError:
                        pass

        # Load average
        for la in ts_elem.iter(f"{ns}load-average"):
            for key in ("load1", "load5", "load15"):
                val = la.get(key)
                if val is not None:
                    try:
                        yield (ts, key, float(val), hostname)
                    except ValueError:
                        pass


def parse_sar_xml_files(sos_root: str) -> Iterator[tuple[int, str, float, str]]:
    """Generator: yield metrics from all SAR XML files."""
    sar_xml_dir = Path(sos_root) / "sos_commands" / "sar"
    if not sar_xml_dir.exists():
        return
    for f in sorted(sar_xml_dir.glob("sa*.xml")):
        yield from _parse_sar_xml_file(f)


# ============================================================
# Binary fallback: use sadf
# ============================================================

def _parse_sar_binary_sadf(sa_file: Path) -> list[dict]:
    """Parse a single saXX binary file via sadf -j."""
    if not sa_file.exists():
        return []

    try:
        proc = subprocess.run(
            ["sadf", "-j", str(sa_file), "--", "-A"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []

        data = json.loads(proc.stdout)
        return _extract_sadf_metrics(data)
    except FileNotFoundError:
        return []
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def _extract_sadf_metrics(sadf_data: dict) -> list[dict]:
    """Flatten sadf JSON output into metric dicts."""
    results: list[dict] = []

    for host_entry in sadf_data.get("sysstat", {}).get("hosts", []):
        host = host_entry.get("nodename", "")
        for stat in host_entry.get("statistics", []):
            ts = stat.get("timestamp", {}).get("epoch", 0)
            if not ts:
                continue

            for cpu_load in stat.get("cpu-load", []):
                for key in ("user", "nice", "system", "iowait", "steal", "idle", "softirq"):
                    if key in cpu_load and cpu_load[key] is not None:
                        results.append({
                            "metric": f"cpu_{key}", "timestamp": ts,
                            "value": float(cpu_load[key]), "host": host,
                        })

            mem = stat.get("memory", {})
            for key in ("memfree", "memused", "swapfree", "swapused"):
                if key in mem and mem[key] is not None:
                    results.append({
                        "metric": f"mem_{key}", "timestamp": ts,
                        "value": float(mem[key]), "host": host,
                    })

            for disk in stat.get("disk", []):
                dname = disk.get("disk-device", "")
                for key in ("await", "util", "svctm", "avgqu-sz"):
                    if key in disk and disk[key] is not None:
                        results.append({
                            "metric": f"io_{dname}_{key}", "timestamp": ts,
                            "value": float(disk[key]), "host": host,
                        })

            for net in stat.get("network", {}).get("net-dev", []):
                iface = net.get("iface", "")
                for key in ("rxdrop", "txdrop", "rxerr", "txerr", "rxpck", "txpck"):
                    if key in net and net[key] is not None:
                        results.append({
                            "metric": f"net_{iface}_{key}", "timestamp": ts,
                            "value": float(net[key]), "host": host,
                        })

    return results


def parse_sar_binary_files(sos_root: str) -> Iterator[tuple[int, str, float, str]]:
    """Generator: yield metrics from saXX binary files via sadf."""
    candidates = [
        Path(sos_root) / "var" / "log" / "sa",
        Path(sos_root) / "sos_commands" / "sar",
    ]

    for sar_dir in candidates:
        if not sar_dir.exists():
            continue
        for f in sorted(sar_dir.iterdir()):
            if not re.match(r"sa\d+$", f.name):
                continue
            for m in _parse_sar_binary_sadf(f):
                yield (m["timestamp"], m["metric"], m["value"], m.get("host", ""))


# ============================================================
# Unified entry: XML first -> binary fallback
# ============================================================

def parse_sar_files(sos_root: str) -> Iterator[tuple[int, str, float, str]]:
    """Generator: yield (timestamp, metric, value, host) tuples.

    Priority:
    1. sos_commands/sar/saXX.xml  (XML format, auto-converted by sosreport)
    2. var/log/sa/saXX            (binary format, requires sadf)
    """
    sar_xml_dir = Path(sos_root) / "sos_commands" / "sar"

    # Try XML first
    if sar_xml_dir.exists() and list(sar_xml_dir.glob("sa*.xml")):
        count = 0
        for f in sorted(sar_xml_dir.glob("sa*.xml")):
            for item in _parse_sar_xml_file(f):
                yield item
                count += 1
        print(f"  SAR: parsed {count:,} metrics from XML files")
        return

    # Fallback to binary sadf
    count = 0
    candidates = [
        Path(sos_root) / "var" / "log" / "sa",
        Path(sos_root) / "sos_commands" / "sar",
    ]
    found = False
    for sar_dir in candidates:
        if not sar_dir.exists():
            continue
        for f in sorted(sar_dir.iterdir()):
            if not re.match(r"sa\d+$", f.name):
                continue
            for m in _parse_sar_binary_sadf(f):
                yield (m["timestamp"], m["metric"], m["value"], m.get("host", ""))
                count += 1
                found = True

    if found:
        print(f"  SAR: parsed {count:,} metrics from binary files (via sadf)")
    else:
        print("  SAR: no SAR data found (neither XML nor binary)")
