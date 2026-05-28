"""RCA Evaluation Framework — 自动评估 RCA 正确率。

遍历 evaluation/cases/ 下所有测试案例，运行完整 Pipeline，
对比 expected.json 打分，输出 JSON 评估报告。
"""
import json
import sys
import time
import tempfile
from datetime import datetime
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.extractor import extract_and_index
from src.timeline import build_timeline
from src.rules import run_rule_engine
from src.rca import run_rca
from src.models import StructuredEvidence


def evaluate_case(case_dir: Path, llm_config: dict) -> dict:
    """对单个案例运行完整 Pipeline，对比 expected.json 打分。"""
    expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
    sosreport = case_dir / "sosreport.tar.xz"
    if not sosreport.exists():
        return {"case": expected["scenario"], "error": "sosreport.tar.xz not found"}

    # 使用临时 workspace，避免交叉污染
    with tempfile.TemporaryDirectory(prefix="eval_ws_") as workspace:
        # Step 1: Extract
        manifest = extract_and_index(str(sosreport), workspace)

        # Step 2: Timeline
        window = build_timeline(manifest, expected["fault_time"], workspace, margin_minutes=15)

        # Step 3: Rule Engine
        evidence = run_rule_engine(workspace, expected["fault_description"],
                                    expected["fault_time"], window)

        # Step 4: RCA (LLM)
        rca = run_rca(evidence, llm_config)

        # Step 5: Score
        return _score(expected, evidence, rca)


def _score(expected: dict, evidence: StructuredEvidence, rca) -> dict:
    """对比预期和实际输出，计算分数。"""
    detected_events = {e.event_type for e in evidence.events}
    root_cause_text = (rca.root_cause + " " + rca.summary).lower()

    # 事件召回率
    must = expected.get("must_have_events", [])
    must_hit = sum(1 for e in must if e in detected_events)
    event_recall = must_hit / len(must) if must else 1.0

    # 禁止事件违规
    forbidden = expected.get("forbidden_events", [])
    forbidden_hit = [e for e in forbidden if e in detected_events]
    forbidden_penalty = len(forbidden_hit) * 0.1

    # 关键词召回
    must_kw = expected.get("must_have_keywords", [])
    must_kw_hit = sum(1 for kw in must_kw if kw.lower() in root_cause_text)
    kw_recall = must_kw_hit / len(must_kw) if must_kw else 1.0

    # 禁止关键词违规
    forbidden_kw = expected.get("forbidden_keywords", [])
    forbidden_kw_hit = [kw for kw in forbidden_kw if kw.lower() in root_cause_text]
    kw_penalty = len(forbidden_kw_hit) * 0.15

    # 综合分数
    score = max(0.0, event_recall * 0.6 + kw_recall * 0.4 - forbidden_penalty - kw_penalty)

    # 额外检查
    should_contain = expected.get("root_cause_should_contain", "")
    should_not_contain = expected.get("root_cause_should_not_contain", "")
    extra_checks = {}
    if should_contain and should_contain.lower() not in root_cause_text:
        extra_checks["root_cause_should_contain"] = f"'{should_contain}' not found in RCA"
    if should_not_contain and should_not_contain.lower() in root_cause_text:
        extra_checks["root_cause_should_not_contain"] = f"'{should_not_contain}' found in RCA"

    return {
        "case": expected["scenario"],
        "score": round(score, 2),
        "event_recall": round(event_recall, 2),
        "keyword_recall": round(kw_recall, 2),
        "forbidden_event_violations": forbidden_hit,
        "forbidden_keyword_violations": forbidden_kw_hit,
        "root_cause": rca.root_cause[:300],
        "summary": rca.summary[:300],
        "detected_events": sorted(detected_events),
        "missing_events": [e for e in must if e not in detected_events],
        "extra_checks": extra_checks,
        "event_count_before_agg": len(evidence.events),
    }


def evaluate_all(cases_root: str, llm_config: dict | None = None) -> dict:
    """遍历所有案例，输出汇总报告。"""
    if llm_config is None:
        llm_config = {}

    results = []
    cases_root = Path(cases_root)
    for case_dir in sorted(cases_root.iterdir()):
        if not case_dir.is_dir():
            continue
        expected_file = case_dir / "expected.json"
        if not expected_file.exists():
            continue

        print(f"\n{'='*50}")
        print(f"Evaluating: {case_dir.name}")
        print(f"{'='*50}")

        t0 = time.time()
        result = evaluate_case(case_dir, llm_config)
        elapsed = time.time() - t0

        if "error" in result:
            print(f"  ERROR: {result['error']}")
        else:
            status = "PASS" if result["score"] >= 0.70 else "FAIL"
            print(f"  [{status}] score={result['score']:.2f}, "
                  f"event_recall={result['event_recall']:.2f}, "
                  f"kw_recall={result['keyword_recall']:.2f}")
            print(f"  Root cause: {result['root_cause'][:150]}...")
            if result["missing_events"]:
                print(f"  Missing events: {result['missing_events']}")
            if result["forbidden_event_violations"]:
                print(f"  Forbidden events: {result['forbidden_event_violations']}")
        print(f"  Time: {elapsed:.1f}s")
        results.append(result)

    if not results:
        return {"error": "No cases found", "total_cases": 0}

    scored = [r for r in results if "score" in r]
    avg_score = sum(r["score"] for r in scored) / len(scored) if scored else 0.0

    report = {
        "timestamp": datetime.now().isoformat(),
        "total_cases": len(results),
        "average_score": round(avg_score, 2),
        "pass_count": sum(1 for r in scored if r["score"] >= 0.70),
        "fail_count": sum(1 for r in scored if r["score"] < 0.70),
        "results": results,
    }

    # 保存报告
    out_dir = cases_root.parent / "reports"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"eval_{ts}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # 控制台汇总
    print(f"\n{'='*50}")
    print(f"EVALUATION SUMMARY")
    print(f"{'='*50}")
    print(f"Total cases: {len(results)}")
    print(f"Average score: {avg_score:.2f}")
    print(f"Pass (>=0.70): {report['pass_count']} / Fail: {report['fail_count']}")
    print(f"Report saved: {out_file}")

    return report


if __name__ == "__main__":
    import os

    api_key = os.environ.get("DEEPSEEK_API_KEY", "sk-0dfee4a774094c2a9ec86b8961043bff")
    api_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")

    cases_root = Path(__file__).parent / "cases"
    evaluate_all(str(cases_root), {
        "api_key": api_key,
        "api_url": api_url,
        "model": model,
    })
