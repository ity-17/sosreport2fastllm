"""Rule Engine: keyword rules + threshold rules -> StructuredEvidence.

Reads events within time window from SQLite, runs rules, outputs structured anomaly evidence.
"""
import sqlite3
from pathlib import Path
from src.models import RuleEvent, StructuredEvidence
from src.rules.keyword_rules import match_keyword_rules
from src.rules.threshold_rules import run_threshold_rules


def _connect_readonly(workspace: str) -> sqlite3.Connection:
    db_path = str(Path(workspace) / "timeline.db")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _assess_quality(workspace: str, start_ts: int, end_ts: int) -> str:
    """Deterministic data quality assessment. Checks 4 data sources."""
    try:
        con = _connect_readonly(workspace)
    except Exception:
        return "LOW"

    checks = {
        "syslog_data": con.execute(
            "SELECT COUNT(*) FROM events WHERE source LIKE '%messages%'"
        ).fetchone()[0],
        "journal_data": con.execute(
            "SELECT COUNT(*) FROM events WHERE source LIKE '%journal%'"
        ).fetchone()[0],
        "dmesg_data": con.execute(
            "SELECT COUNT(*) FROM events WHERE source LIKE '%dmesg%'"
        ).fetchone()[0],
        "sar_data": con.execute(
            "SELECT COUNT(*) FROM metrics WHERE timestamp BETWEEN ? AND ?",
            (start_ts, end_ts),
        ).fetchone()[0],
    }
    con.close()

    passed = sum(1 for v in checks.values() if v > 0)

    if passed >= 3:
        return "HIGH"
    elif passed >= 2:
        return "MEDIUM"
    else:
        return "LOW"


def run_rule_engine(workspace: str, fault_description: str,
                    fault_time: str, window: tuple[int, int]) -> StructuredEvidence:
    """Read window data from DB, run rules, return structured evidence."""
    start_ts, end_ts = window
    con = _connect_readonly(workspace)

    # 1. Batch-read events in window (fetchmany avoids loading all rows at once)
    cursor = con.execute(
        "SELECT timestamp, source, line FROM events "
        "WHERE timestamp >= ? AND timestamp <= ? AND timestamp > 0 "
        "ORDER BY timestamp",
        (start_ts, end_ts),
    )

    # 2. Run keyword rules (process 5000 rows at a time)
    rule_events: list[RuleEvent] = []
    seen = set()

    while True:
        batch = cursor.fetchmany(5000)
        if not batch:
            break
        for ts, source, line in batch:
            matches = match_keyword_rules(line)
            for event_type, severity in matches:
                dedup_key = (event_type, source, ts)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                rule_events.append(RuleEvent(
                    event_type=event_type,
                    timestamp=ts if ts > 0 else None,
                    severity=severity,
                    source_file=source,
                    evidence={"raw_line": line[:300]},
                ))

    # 3. Run threshold rules
    threshold_events = run_threshold_rules(workspace, start_ts, end_ts)
    for te in threshold_events:
        rule_events.append(RuleEvent(
            event_type=te["event_type"],
            severity=te["severity"],
            evidence=te["evidence"],
        ))

    con.close()

    # 4. Sort by severity: CRITICAL > HIGH > MEDIUM > LOW
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    rule_events.sort(key=lambda e: severity_order.get(e.severity, 3))

    # 5. Assess data quality
    quality = _assess_quality(workspace, start_ts, end_ts)

    return StructuredEvidence(
        fault_description=fault_description,
        fault_time=fault_time,
        time_window=window,
        events=rule_events,
        data_quality=quality,
    )
