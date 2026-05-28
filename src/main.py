"""SOSReport RCA Engine V0.2 — 主入口."""
import argparse
import os
import sys
import time
from pathlib import Path

# Ensure src/ is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import DEEPSEEK_API_KEY, DEEPSEEK_API_URL, LLM_MODEL, DEFAULT_MARGIN
from src.extractor import extract_and_index
from src.timeline import build_timeline
from src.rules import run_rule_engine
from src.rca import run_rca
from src.report import generate_report


def main():
    parser = argparse.ArgumentParser(
        description="SOSReport RCA Engine V0.1 — Linux SOSReport AI 诊断工具",
    )
    parser.add_argument("sosreport", help="sosreport archive path or directory")
    parser.add_argument("--fault-desc", required=True, help='故障描述，如 "15:32 系统卡死"')
    parser.add_argument("--fault-time", help='故障时间点，如 "15:32" 或 "2024-01-15 15:32:00"')
    parser.add_argument("--output", default="report.md", help="输出报告路径 (default: report.md)")
    parser.add_argument("--workspace", default="./workspace", help="工作目录 (default: ./workspace)")
    parser.add_argument("--margin", type=int, default=DEFAULT_MARGIN, help=f"时间窗口前后分钟数 (default: {DEFAULT_MARGIN})")
    parser.add_argument("--model", default=LLM_MODEL, help=f"LLM 模型 (default: {LLM_MODEL})")
    parser.add_argument("--no-llm", action="store_true", help="不使用 LLM，仅输出规则引擎证据")
    parser.add_argument("--api-key", help="DeepSeek API key（也可设环境变量 DEEPSEEK_API_KEY）")
    parser.add_argument("--api-url", help="API 地址（默认 https://api.deepseek.com，也可设环境变量 DEEPSEEK_BASE_URL）")
    args = parser.parse_args()

    t0 = time.time()

    # 如果没给故障时间，用 fault_desc 中的第一个时间
    fault_time = args.fault_time
    if not fault_time:
        import re
        m = re.search(r'(\d{1,2}:\d{2})', args.fault_desc)
        if m:
            fault_time = m.group(1)
        else:
            fault_time = args.fault_desc

    print(f"=" * 60)
    print(f"SOSReport RCA Engine V0.1")
    print(f"=" * 60)
    print(f"SOSReport: {args.sosreport}")
    print(f"Fault: {args.fault_desc}")
    print(f"Fault time: {fault_time}")
    print(f"Margin: ±{args.margin} min")
    print()

    # Step 1: Extractor
    print("[1/5] Extracting sosreport...")
    t1 = time.time()
    manifest = extract_and_index(args.sosreport, args.workspace)
    print(f"  Found {manifest.total_files} files (step took {time.time() - t1:.1f}s)")
    print()

    # Step 2: Timeline
    print("[2/5] Building timeline...")
    t2 = time.time()
    window = build_timeline(manifest, fault_time, args.workspace, margin_minutes=args.margin)
    print(f"  Time window: {window} (step took {time.time() - t2:.1f}s)")
    print()

    # Step 3: Rule Engine
    print("[3/5] Running rule engine...")
    t3 = time.time()
    evidence = run_rule_engine(args.workspace, args.fault_desc, fault_time, window)
    print(f"  Detected {len(evidence.events)} abnormal events")
    print(f"  Data quality: {evidence.data_quality} (step took {time.time() - t3:.1f}s)")
    print()

    if args.no_llm:
        # 只输出规则引擎结果
        print("=" * 60)
        print("Rule Engine Results (no LLM)")
        print("=" * 60)
        for e in evidence.events:
            print(f"  [{e.severity:8s}] {e.event_type:20s} | {e.evidence}")
        print()
        return

    # Step 4: RCA
    print("[4/5] Running RCA analysis (LLM)...")
    t4 = time.time()
    rca = run_rca(evidence, {
        "model": args.model,
        "api_key": args.api_key or DEEPSEEK_API_KEY,
        "api_url": args.api_url or DEEPSEEK_API_URL,
    })
    print(f"  Root cause: {rca.root_cause} (step took {time.time() - t4:.1f}s)")
    print()

    # Step 5: Report
    print(f"[5/5] Generating report...")
    t5 = time.time()
    report_path = generate_report(rca, evidence, args.output)
    print(f"  Report saved to: {report_path} (step took {time.time() - t5:.1f}s)")
    print()

    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s")
    print()


if __name__ == "__main__":
    main()
