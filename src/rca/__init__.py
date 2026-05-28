"""RCA LLM Agent —— 系统唯一 LLM 调用点。

接收 StructuredEvidence，调用 LLM 推理根因，返回 RCAOutput。
"""
import json
import os
import re
from datetime import datetime
from src.models import StructuredEvidence, RCAOutput
from src.config import DEEPSEEK_API_KEY, DEEPSEEK_API_URL, LLM_MODEL

RCA_SYSTEM_PROMPT = """你是一名资深 Linux SRE 专家，擅长从系统异常事件中推理故障根因。

## 输入说明

你收到的输入是规则引擎提取的结构化异常事件列表。事件格式为：
  [严重度] 事件类型 | 证据详情 | (出现次数, 时间范围)

事件中的 count 字段表示该事件在时间窗口内出现了多少次。高频事件（count > 100）通常是根因的直接信号。

## 推理要求

### 1. 根因推断
- 区分「根因」和「表象」：OOM Kill 通常是表象，根因可能是内存泄漏、存储 I/O Hang、或配置不当
- 根因必须有具体证据支撑，禁止凭空推测
- 每个根因声明必须引用至少 1 条事件证据，格式：「[证据] 事件类型 → 推断」

### 2. 竞争性假设（必填）
- 列出至少 1 个被考虑的竞争性假设
- 说明为何排除该假设（必须引用具体缺失的证据或矛盾的事件）
- 格式：
  {
    "hypothesis": "CPU 软死锁",
    "why_rejected": "未检测到 SOFT_LOCKUP 事件，且 SAR 显示 CPU idle 正常"
  }

### 3. 证据不足声明
- 如果某个推理步骤缺乏直接证据，必须声明「证据不足」
- 不允许用模糊表述掩盖信息缺失

### 4. 修复建议
- 按优先级排序（P0 → P1 → P2）
- 每条建议必须是可执行的具体操作，如「检查 SAN 交换机端口错误计数」
- 禁止泛化建议如「排查存储问题」

## 输出格式

严格 JSON，不要包含 markdown 代码块标记。字段说明：
- root_cause: 1-2 句话精确描述根因
- summary: 2-3 句话完整故障过程
- timeline: [{time, event}]
- evidence: [{event_type, inference}]  每个必须是「事件 → 我的推断」
- impact: 影响了哪些服务/节点/用户
- alternative_hypotheses: [{hypothesis, why_rejected}]
- recommendations: [{priority, action}]  P0/P1/P2

## 禁止事项

- 编造不存在的事件作为证据
- 使用模糊表述如「可能是系统问题」「硬件故障」
- 忽视数据质量标记（MEDIUM/LOW 时必须降低置信度表述）
- 把表象当根因（OOM Kill 本身不是根因）
"""


def _build_prompt(evidence: StructuredEvidence) -> str:
    """从 StructuredEvidence 构建 LLM prompt。支持聚合事件的展示。"""
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}

    # 按严重度 + 出现次数降序排列
    def sort_key(e):
        count = e.evidence.get("count", 1) if isinstance(e.evidence, dict) else 1
        return (severity_order.get(e.severity, 3), -count)

    sorted_events = sorted(evidence.events, key=sort_key)

    # 构建事件列表
    events_str_parts = []
    for e in sorted_events:
        sev = e.severity
        et = e.event_type
        ev = e.evidence

        if isinstance(ev, dict) and "count" in ev and ev["count"] > 1:
            first = datetime.fromtimestamp(ev["first_seen"]).strftime("%H:%M:%S") if ev.get("first_seen") else "?"
            last = datetime.fromtimestamp(ev["last_seen"]).strftime("%H:%M:%S") if ev.get("last_seen") else "?"
            samples = ""
            if ev.get("sample_lines"):
                samples = "\n    样本: " + ev["sample_lines"][0][:120]
            events_str_parts.append(
                f"[{sev}] {et} | 出现 {ev['count']} 次, {first} ~ {last}{samples}"
            )
        elif isinstance(ev, dict) and "raw_line" in ev:
            events_str_parts.append(f"[{sev}] {et} | {ev['raw_line'][:200]}")
        else:
            events_str_parts.append(f"[{sev}] {et} | {json.dumps(ev, ensure_ascii=False)[:200]}")

    events_str = "\n".join(events_str_parts)

    # 构建时间线（聚合事件用 first_seen）
    timeline_events = []
    for e in evidence.events:
        ts = None
        if isinstance(e.evidence, dict) and "first_seen" in e.evidence:
            ts = e.evidence["first_seen"]
        elif e.timestamp:
            ts = e.timestamp
        if ts:
            timeline_events.append((ts, e))

    timeline_events.sort(key=lambda x: x[0])
    timeline_str = "\n".join(
        f"- {datetime.fromtimestamp(ts).strftime('%H:%M:%S')} [{e.severity}] {e.event_type}"
        for ts, e in timeline_events[:30]
    )

    # 严重度统计
    severity_count = {}
    for e in evidence.events:
        severity_count[e.severity] = severity_count.get(e.severity, 0) + 1

    # 数据质量警告
    quality_warning = ""
    if evidence.data_quality == "MEDIUM":
        quality_warning = "\n\n⚠️ 数据质量为 MEDIUM，部分日志源缺失。结论可信度受限，必须在报告中标注不确定性。"
    elif evidence.data_quality == "LOW":
        quality_warning = "\n\n⚠️ 数据质量为 LOW，关键日志严重缺失。结论仅供参考，建议补充数据后重新分析。"

    return f"""## 故障描述
{evidence.fault_description}

## 故障时间窗口
{evidence.fault_time}（±15 分钟）

## 数据质量
{evidence.data_quality}{quality_warning}

## 检测到的异常事件（按严重度和频率排序，共 {len(evidence.events)} 个类型）

### 严重度统计
CRITICAL: {severity_count.get('CRITICAL', 0)}
HIGH: {severity_count.get('HIGH', 0)}
MEDIUM: {severity_count.get('MEDIUM', 0)}
LOW: {severity_count.get('LOW', 0)}

### 事件详情
{events_str}

### 事件时间线
{timeline_str}

请以 JSON 格式输出 RCA 分析结果。"""


def _extract_json(text: str) -> dict:
    """从 LLM 响应中提取 JSON。容错处理非标准格式。"""
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 尝试提取 ```json ... ``` 代码块
    m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试提取 { ... } 最外层的 JSON
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # 完全失败：返回降级结构
    return {
        "root_cause": "无法解析 LLM 输出",
        "summary": text[:500],
        "timeline": [],
        "evidence": [],
        "impact": "",
        "alternative_hypotheses": [],
        "recommendations": [],
    }


def run_rca(evidence: StructuredEvidence, llm_config: dict | None = None) -> RCAOutput:
    """调用 LLM 进行根因分析。

    Args:
        evidence: 结构化证据
        llm_config: {"model": "claude-sonnet-4-6", "api_key": "..."}
                    不传则使用环境变量 ANTHROPIC_API_KEY
    """
    if llm_config is None:
        llm_config = {}

    prompt = _build_prompt(evidence)

    try:
        from openai import OpenAI

        api_key = llm_config.get("api_key") or DEEPSEEK_API_KEY
        api_url = llm_config.get("api_url") or DEEPSEEK_API_URL
        client = OpenAI(api_key=api_key, base_url=api_url)

        model = llm_config.get("model", LLM_MODEL)
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": RCA_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        # deepseek-reasoner: 内部思考消耗 token，需要更大的输出额度，且不支持 temperature
        if "reasoner" in model:
            kwargs["max_tokens"] = 8192
        else:
            kwargs["max_tokens"] = 4096
            kwargs["temperature"] = 0.1

        response = client.chat.completions.create(**kwargs)

        text = response.choices[0].message.content if response.choices else ""
        result = _extract_json(text or "")

        # 如果 JSON 解析失败，保留原始文本用于排查
        if result.get("root_cause") == "无法解析 LLM 输出":
            print(f"[RCA] JSON 解析失败，LLM 原始响应（前 500 字符）: {text[:500]}", flush=True)

    except Exception as exc:
        # LLM 不可用时降级为基于规则的推断
        import traceback
        print(f"[RCA] LLM 调用异常: {exc}", flush=True)
        traceback.print_exc()
        result = _fallback_rca(evidence, str(exc))

    # 归一化 LLM 输出（兼容 dict/list 混合格式）
    def _norm_strings(items: list) -> list[str]:
        out = []
        for item in items:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                # timeline: {"time": "16:14", "event": "xxx"}
                if "time" in item:
                    out.append(f"{item.get('time', '?')} - {item.get('event', item.get('description', ''))}")
                # evidence: {"event_type": "OOM_KILL", "inference": "..."}
                elif "event_type" in item:
                    out.append(f"[{item.get('event_type', '?')}] {item.get('inference', '')}")
                # alternative_hypotheses: {"hypothesis": "...", "why_rejected": "..."}
                elif "hypothesis" in item:
                    h = item.get('hypothesis', '')
                    w = item.get('why_rejected', '')
                    out.append(f"假设: {h} | 排除原因: {w}")
                # recommendations: {"priority": "P0", "action": "xxx"} or {"priority": 0, "action": "xxx"}
                elif "action" in item:
                    p = str(item.get('priority', '?'))
                    if p.startswith("P") or p.startswith("p"):
                        out.append(f"[{p.upper()}] {item['action']}")
                    else:
                        out.append(f"[P{p}] {item['action']}")
                else:
                    out.append(str(item))
            else:
                out.append(str(item))
        return out

    return RCAOutput(
        root_cause=result.get("root_cause", ""),
        summary=result.get("summary", ""),
        timeline=_norm_strings(result.get("timeline", [])),
        evidence=_norm_strings(result.get("evidence", [])),
        impact=result.get("impact", ""),
        alternative_hypotheses=_norm_strings(result.get("alternative_hypotheses", [])),
        recommendations=_norm_strings(result.get("recommendations", [])),
    )


def _fallback_rca(evidence: StructuredEvidence, error: str) -> dict:
    """LLM 不可用时的降级规则推断。"""
    event_types = {e.event_type for e in evidence.events}
    root_cause = "无法确定（LLM 不可用）"
    summary = f"LLM 调用失败: {error}。以下为基于规则的初步推断。"

    # 简单规则推断
    if "IO_ERROR" in event_types and "BLOCKED_TASK" in event_types:
        root_cause = "存储 I/O 异常导致大量 D 状态进程（blocked task）"
        summary += " 检测到 IO_ERROR + BLOCKED_TASK 组合，最可能为存储链路故障。"
    elif "OOM_KILL" in event_types and "BLOCKED_TASK" in event_types:
        root_cause = "存储 I/O Hang 导致内存无法回收，触发 OOM Kill"
        summary += " IO hang → 进程 D 状态 → 内存压力积累 → OOM Killer。"
    elif "OOM_KILL" in event_types:
        root_cause = "内存耗尽触发 OOM Killer"
    elif "SOFT_LOCKUP" in event_types:
        root_cause = "CPU 软死锁"
    else:
        root_cause = "证据不足，无法自动推断"

    return {
        "root_cause": root_cause,
        "summary": summary,
        "timeline": [f"{e.event_type} (severity={e.severity})" for e in evidence.events[:10]],
        "evidence": [f"{e.event_type}: {str(e.evidence)[:200]}" for e in evidence.events[:10]],
        "impact": "待 LLM 分析",
        "alternative_hypotheses": [],
        "recommendations": ["LLM 不可用，建议人工分析"],
    }
