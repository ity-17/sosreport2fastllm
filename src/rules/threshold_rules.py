"""Threshold rules: query aggregated values from SQLite metrics and compare to thresholds.

Each rule: (metric, agg, threshold, op, event_type, severity)

Threshold expressions support variables:
  - cores: CPU core count
  - memtotal / memtotal_kb: total memory in KB
"""
import sqlite3
from pathlib import Path

THRESHOLD_RULES: list[tuple] = [
    # (metric, agg, threshold, op, event_type, severity)
    ("cpu_iowait",   "max",  50,   ">",  "HIGH_IOWAIT",       "HIGH"),
    ("cpu_iowait",   "avg",  30,   ">",  "SUSTAINED_IOWAIT",  "MEDIUM"),
    ("cpu_steal",    "max",  30,   ">",  "CPU_STEAL",         "HIGH"),
    ("cpu_iowait",   "max",  80,   ">",  "EXTREME_IOWAIT",    "CRITICAL"),
    ("mem_memused",  "max",  90,   ">",  "MEMORY_PRESSURE",   "HIGH"),
    ("mem_swapused", "max",  50,   ">",  "SWAP_PRESSURE",     "MEDIUM"),
    ("io_await",     "max",  5000, ">",  "IO_LATENCY",        "CRITICAL"),
    ("io_await",     "avg",  1000, ">",  "IO_SLOW",           "HIGH"),
    ("io_util",      "max",  90,   ">",  "IO_SATURATION",     "CRITICAL"),
    ("mem_free",     "min",  "memtotal*0.05",  "<",  "MEMORY_EXHAUSTION", "CRITICAL"),
]


def _connect_readonly(workspace: str) -> sqlite3.Connection:
    db_path = str(Path(workspace) / "timeline.db")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _resolve_threshold(threshold, cores: int = 8, memtotal_kb: int = 16777216) -> float:
    """Resolve threshold expression. Supports numbers and expression strings."""
    if isinstance(threshold, (int, float)):
        return float(threshold)
    if isinstance(threshold, str):
        try:
            return float(eval(threshold, {"cores": cores, "memtotal_kb": memtotal_kb,
                                           "memtotal": memtotal_kb}))
        except Exception:
            return float("inf")
    return float("inf")


def _query_metric_agg(workspace: str, metric: str, agg: str,
                      start_ts: int, end_ts: int) -> float | None:
    """Query aggregated value for a metric within time window."""
    try:
        con = _connect_readonly(workspace)
        agg_func = {"max": "MAX", "min": "MIN", "avg": "AVG"}.get(agg, "MAX")
        row = con.execute(
            f"SELECT {agg_func}(value) FROM metrics "
            "WHERE metric = ? AND timestamp >= ? AND timestamp <= ?",
            (metric, start_ts, end_ts),
        ).fetchone()
        con.close()
        if row and row[0] is not None:
            return float(row[0])
    except Exception:
        pass
    return None


def _query_memtotal(workspace: str) -> float:
    """Try to get total memory from events or return default."""
    return 16777216.0  # 16GB default


def run_threshold_rules(workspace: str, start_ts: int, end_ts: int,
                        cores: int = 8) -> list[dict]:
    """Run all threshold rules, return list of triggered anomaly events."""
    results = []
    memtotal = _query_memtotal(workspace)

    for metric, agg, threshold, op, event_type, severity in THRESHOLD_RULES:
        value = _query_metric_agg(workspace, metric, agg, start_ts, end_ts)
        if value is None:
            continue

        threshold_val = _resolve_threshold(threshold, cores=cores, memtotal_kb=memtotal)

        triggered = False
        if op == ">":
            triggered = value > threshold_val
        elif op == "<":
            triggered = value < threshold_val
        elif op == ">=":
            triggered = value >= threshold_val
        elif op == "<=":
            triggered = value <= threshold_val

        if triggered:
            results.append({
                "event_type": event_type,
                "severity": severity,
                "evidence": {
                    "metric": metric,
                    "agg": agg,
                    "value": round(value, 2),
                    "threshold": round(threshold_val, 2),
                },
            })

    return results
