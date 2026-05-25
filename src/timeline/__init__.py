"""Timeline Engine: time normalization + time window filtering + SQLite storage.

V0.3: Replaced DuckDB with SQLite (DuckDB executemany has 2.8ms/row latency on Windows).
"""
import re
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional
from src.models import FileManifest, LogType

DMESG_RE = re.compile(r'^\[\s*(\d+\.?\d*)\s*\]')

LOG_TYPES = {LogType.SYSLOG, LogType.JOURNAL, LogType.DMESG, LogType.CONTAINER_LOG}

CONTAINER_TS_RE = re.compile(r'"time"\s*:\s*"([^"]+)"')


def _sample_timestamp_from_files(manifest: FileManifest) -> Optional[int]:
    """Sample timestamps from first few lines of syslog files to infer year."""
    from src.parsers.syslog import parse_syslog_time
    for entry in manifest.entries:
        if entry.log_type != LogType.SYSLOG:
            continue
        try:
            with open(entry.abs_path, errors="ignore") as f:
                for i, line in enumerate(f):
                    if i > 20:
                        break
                    ts = parse_syslog_time(line)
                    if ts:
                        return ts
        except IOError:
            continue
    return None


def _parse_fault_time(fault_time_str: str, ref_ts: Optional[int] = None) -> Optional[int]:
    """Convert user fault time string to UNIX timestamp.

    Supports: "HH:MM", "YYYY-MM-DD HH:MM:SS"
    When only HH:MM is given, infer date from ref_ts (sampled from data).
    """
    if re.match(r'^\d{1,2}:\d{2}$', fault_time_str):
        parts = fault_time_str.split(":")
        hour, minute = int(parts[0]), int(parts[1])
        ref_dt = datetime.fromtimestamp(ref_ts) if ref_ts else datetime.now()
        dt = ref_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return int(dt.timestamp())

    if re.match(r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}', fault_time_str):
        try:
            dt = datetime.strptime(fault_time_str[:19], "%Y-%m-%d %H:%M:%S")
            return int(dt.timestamp())
        except ValueError:
            pass

    return None


def _extract_timestamp(line: str, entry, year: int, boot_time: Optional[int] = None) -> Optional[int]:
    """Extract UNIX timestamp from a log line."""
    from src.parsers.syslog import parse_syslog_time

    if entry.log_type == LogType.SYSLOG:
        return parse_syslog_time(line, year)

    if entry.log_type == LogType.CONTAINER_LOG:
        m = CONTAINER_TS_RE.search(line)
        if m:
            try:
                dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                return int(dt.timestamp())
            except (ValueError, OSError):
                pass
        return None

    if entry.log_type == LogType.DMESG and boot_time:
        m = DMESG_RE.match(line)
        if m:
            try:
                return int(boot_time + float(m.group(1)))
            except ValueError:
                pass

    return None


def build_timeline(manifest: FileManifest, fault_time_str: str,
                   workspace: str, margin_minutes: int = 15) -> tuple[int, int]:
    """Build timeline: write all log events into SQLite, return time window.

    Only processes log-type files (SYSLOG/JOURNAL/DMESG/CONTAINER_LOG),
    skips UNKNOWN (config files, command outputs, etc.).

    Returns: (window_start_ts, window_end_ts)
    """
    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    db_path = str(ws / "timeline.db")

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            timestamp INTEGER, source TEXT, line TEXT,
            host TEXT DEFAULT '', severity TEXT DEFAULT 'INFO'
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS metrics (
            timestamp INTEGER, metric TEXT, value REAL, host TEXT DEFAULT ''
        )
    """)
    con.execute("DELETE FROM events")
    con.execute("DELETE FROM metrics")

    # Sample timestamp to infer year
    ref_ts = _sample_timestamp_from_files(manifest)
    year = datetime.fromtimestamp(ref_ts).year if ref_ts else datetime.now().year

    # Pre-compute dmesg boot_time
    from src.parsers.dmesg import get_boot_time
    sos_root_dir = Path(manifest.entries[0].abs_path) if manifest.entries else Path(workspace)
    if manifest.entries:
        sos_root_dir = Path(manifest.entries[0].abs_path)
        while sos_root_dir.parent != sos_root_dir:
            if (sos_root_dir / "sos_commands").is_dir():
                break
            sos_root_dir = sos_root_dir.parent
    boot_time = get_boot_time(str(sos_root_dir))

    # Only process log-type files
    log_entries = [e for e in manifest.entries if e.log_type in LOG_TYPES]
    print(f"  Processing {len(log_entries)} log files (skipped {manifest.total_files - len(log_entries)} non-log files)")

    total_lines = 0
    for entry in log_entries:
        file_path = Path(entry.abs_path)
        if not file_path.exists():
            continue

        try:
            with open(file_path, errors="ignore") as f:
                rows: list[tuple] = []
                for line in f:
                    line = line.rstrip("\n\r")
                    if not line:
                        continue
                    ts = _extract_timestamp(line, entry, year, boot_time)
                    if ts is None:
                        ts = -1
                    rows.append((ts, entry.path, line, "", "INFO"))

                    if len(rows) >= 5000:
                        con.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)
                        total_lines += len(rows)
                        rows = []

                if rows:
                    con.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)
                    total_lines += len(rows)
        except IOError:
            continue

    # Parse SAR metrics (generator feeds directly into SQLite, no in-memory list)
    try:
        from src.parsers.sar import parse_sar_files
        sar_metrics = parse_sar_files(str(sos_root_dir))
        con.executemany("INSERT INTO metrics VALUES (?, ?, ?, ?)", sar_metrics)
    except Exception:
        pass

    con.commit()

    print(f"  Inserted {total_lines:,} event lines into SQLite")

    # Calculate time window
    approx_fault_ts = _parse_fault_time(fault_time_str, ref_ts)

    if approx_fault_ts:
        window_start = approx_fault_ts - margin_minutes * 60
        window_end = approx_fault_ts + margin_minutes * 60
    else:
        result = con.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM events WHERE timestamp > 0"
        ).fetchone()
        if result and result[0]:
            window_start = int(result[0])
            window_end = int(result[1])
        else:
            window_start = 0
            window_end = 2**31

    con.close()
    return window_start, window_end


# ============================================================
# Query interface
# ============================================================

def _connect_readonly(workspace: str) -> sqlite3.Connection:
    db_path = str(Path(workspace) / "timeline.db")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def get_events_in_window(workspace: str, start_ts: int, end_ts: int) -> list[dict]:
    """Read events within time window from SQLite."""
    con = _connect_readonly(workspace)
    rows = con.execute(
        "SELECT timestamp, source, line, host FROM events "
        "WHERE timestamp >= ? AND timestamp <= ? AND timestamp > 0 "
        "ORDER BY timestamp",
        (start_ts, end_ts),
    ).fetchall()
    con.close()
    return [{"timestamp": r[0], "source": r[1], "line": r[2], "host": r[3]} for r in rows]


def get_events_all(workspace: str) -> list[dict]:
    """Read all events (including unparsed timestamps)."""
    con = _connect_readonly(workspace)
    rows = con.execute(
        "SELECT timestamp, source, line, host FROM events ORDER BY timestamp"
    ).fetchall()
    con.close()
    return [{"timestamp": r[0], "source": r[1], "line": r[2], "host": r[3]} for r in rows]


def get_metrics_in_window(workspace: str, start_ts: int, end_ts: int) -> list[dict]:
    """Read metrics within time window from SQLite."""
    con = _connect_readonly(workspace)
    rows = con.execute(
        "SELECT timestamp, metric, value, host FROM metrics "
        "WHERE timestamp >= ? AND timestamp <= ? "
        "ORDER BY timestamp",
        (start_ts, end_ts),
    ).fetchall()
    con.close()
    return [{"timestamp": r[0], "metric": r[1], "value": r[2], "host": r[3]} for r in rows]


def get_metrics_all(workspace: str) -> list[dict]:
    """Read all metrics."""
    con = _connect_readonly(workspace)
    rows = con.execute(
        "SELECT timestamp, metric, value, host FROM metrics ORDER BY timestamp"
    ).fetchall()
    con.close()
    return [{"timestamp": r[0], "metric": r[1], "value": r[2], "host": r[3]} for r in rows]
