"""SOSReport RCA Web 服务 — 拖拽上传 tar.xz，返回分析报告."""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, request, jsonify
from src.config import DEEPSEEK_API_KEY, DEEPSEEK_API_URL, LLM_MODEL
from src.extractor import extract_and_index
from src.timeline import build_timeline
from src.rules import run_rule_engine
from src.rca import run_rca

app = Flask(__name__)


@app.after_request
def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    return response


@app.route("/api/analyze", methods=["POST", "HEAD", "OPTIONS"])
def analyze():
    if request.method in ("HEAD", "OPTIONS"):
        return "", 200
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "请上传 sosreport 文件"}), 400

    fault_desc = request.form.get("fault_desc", "")
    fault_time = request.form.get("fault_time", "")
    margin = int(request.form.get("margin", 15))
    model = request.form.get("model", LLM_MODEL)

    # 保存上传文件
    job_id = uuid.uuid4().hex[:8]
    tmpdir = Path(tempfile.gettempdir()) / f"sosreport_web_{job_id}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    file_path = tmpdir / f.filename
    f.save(str(file_path))

    workspace = str(tmpdir / "ws")

    steps = []
    t_start = time.time()

    try:
        # Step 1: Extract
        t0 = time.time()
        manifest = extract_and_index(str(file_path), workspace)
        steps.append({"step": "解压+索引", "time": round(time.time() - t0, 1),
                       "detail": f"发现 {manifest.total_files} 个文件"})

        # Step 2: Timeline
        t0 = time.time()
        window = build_timeline(manifest, fault_time, workspace, margin_minutes=margin)
        steps.append({"step": "时间线构建", "time": round(time.time() - t0, 1),
                       "detail": f"时间窗口: {window}"})

        # Step 3: Rule Engine
        t0 = time.time()
        evidence = run_rule_engine(workspace, fault_desc, fault_time, window)
        steps.append({"step": "规则引擎", "time": round(time.time() - t0, 1),
                       "detail": f"检测到 {len(evidence.events)} 个异常事件, 数据质量: {evidence.data_quality}"})

        # Step 4: RCA
        t0 = time.time()
        rca = run_rca(evidence, {
            "model": model,
            "api_key": DEEPSEEK_API_KEY,
            "api_url": DEEPSEEK_API_URL,
        })
        steps.append({"step": "LLM 根因分析", "time": round(time.time() - t0, 1),
                       "detail": rca.root_cause[:100]})

        # Step 5: Build report data
        report = {
            "fault_time": fault_time,
            "fault_description": fault_desc,
            "data_quality": evidence.data_quality,
            "total_files": manifest.total_files,
            "abnormal_events": len(evidence.events),
            "root_cause": rca.root_cause,
            "summary": rca.summary,
            "timeline": rca.timeline,
            "evidence_list": rca.evidence,
            "impact": rca.impact,
            "hypotheses": rca.alternative_hypotheses,
            "recommendations": rca.recommendations,
            "severity_breakdown": _count_severity(evidence),
        }

        steps.append({"step": "报告生成", "time": round(time.time() - t0, 1)})

        return jsonify({
            "success": True,
            "report": report,
            "steps": steps,
            "total_time": round(time.time() - t_start, 1),
        })

    except Exception as exc:
        return jsonify({"error": str(exc), "steps": steps}), 500

    finally:
        # 清理临时文件
        try:
            shutil.rmtree(str(tmpdir), ignore_errors=True)
        except Exception:
            pass


def _count_severity(evidence) -> dict:
    counts = {}
    for e in evidence.events:
        counts[e.severity] = counts.get(e.severity, 0) + 1
    return counts



# HTML 页面从 index.html 加载，避免重复维护
_INDEX_PATH = Path(__file__).parent.parent / "index.html"
HTML_PAGE = _INDEX_PATH.read_text(encoding="utf-8") if _INDEX_PATH.exists() else "<html><body><h1>index.html not found</h1></body></html>"



@app.route("/")
def index():
    return HTML_PAGE


if __name__ == "__main__":
    url = "http://localhost:8080"
    print(f"SOSReport RCA Web 服务启动中...")
    print(f"API Key: {'已配置' if DEEPSEEK_API_KEY else '未配置 — LLM 将不可用'}")
    print()
    webbrowser.open(url)
    app.run(host="127.0.0.1", port=8080, debug=False)
