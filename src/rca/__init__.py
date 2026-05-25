"""RCA LLM Agent —— 系统唯一 LLM 调用点。

接收 StructuredEvidence，调用 LLM 推理根因，返回 RCAOutput。
"""
import json
import os
import re
from datetime import datetime
from src.models import StructuredEvidence, RCAOutput

RCA_SYSTEM_PROMPT = """你是一名资深 Linux SRE 专家，擅长从系统异常事件中推理故障根因。

你收到的输入是经过规则引擎提取的结构化异常事件列表，不是原始日志。
你需要基于这些证据进行因果推理。

要求:
1. 推断最可能的根因
2. 用 2-3 句话总结故障过程
3. 按时间顺序列出关键时间线事件
4. 列出关键证据，每条证据关联到具体事件
5. 推断故障影响范围（影响了哪些服务、节点、用户）
6. 列出至少 1 个被排除的竞争性假设，并说明为何排除
7. 给出 3-5 条修复建议，按优先级排序

禁止:
- 编造不存在的证据
- 忽略数据质量标记（MEDIUM/LOW 时必须在报告中降低置信度表述）
- 使用模糊表述如"可能是系统问题"

输出格式为严格的 JSON，不要包含 markdown 代码块标记。"""


def _build_prompt(evidence: StructuredEvidence) -> str:
    """从 StructuredEvidence 构建 LLM prompt。"""
    # 按 severity 分组统计
    severity_count = {}
    for e in evidence.events:
        severity_count[e.severity] = severity_count.get(e.severity, 0) + 1

    # 列出事件（去重显示 event_type，保留首次出现的 evidence）
    seen_types = set()
    unique_events = []
    for e in evidence.events:
        if e.event_type not in seen_types:
            seen_types.add(e.event_type)
            unique_events.append(e)

    events_str = "\n".join(
        f"- [{e.severity}] {e.event_type}"
        + (f" | {json.dumps(e.evidence, ensure_ascii=False)}" if e.evidence else "")
        for e in unique_events
    )

    # 构建完整时间线（按时间排序的事件）
    timeline_events = [e for e in evidence.events if e.timestamp]
    timeline_events.sort(key=lambda e: e.timestamp or 0)
    timeline_str = "\n".join(
        f"- {datetime.fromtimestamp(e.timestamp).strftime('%H:%M:%S') if e.timestamp else '?'} "
        f"[{e.severity}] {e.event_type}"
        for e in timeline_events[:30]  # 限制数量
    )

    return f"""## 故障描述
{evidence.fault_description}

## 故障时间
{evidence.fault_time}

## 数据质量
{evidence.data_quality}
（HIGH=数据充分 / MEDIUM=数据基本可用 / LOW=数据缺失，结论需谨慎）

## 检测到的异常事件（去重，共 {len(evidence.events)} 个事件）

### 按严重度统计
CRITICAL: {severity_count.get('CRITICAL', 0)}
HIGH: {severity_count.get('HIGH', 0)}
MEDIUM: {severity_count.get('MEDIUM', 0)}
LOW: {severity_count.get('LOW', 0)}

### 唯一事件类型
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

        api_key = llm_config.get("api_key") or os.environ.get("DEEPSEEK_API_KEY") or "sk-placeholder"
        api_url = llm_config.get("api_url") or os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
        client = OpenAI(api_key=api_key, base_url=api_url)

        response = client.chat.completions.create(
            model=llm_config.get("model", "deepseek-chat"),
            max_tokens=2048,
            temperature=0.1,
            messages=[
                {"role": "system", "content": RCA_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )

        text = response.choices[0].message.content if response.choices else ""
        result = _extract_json(text or "")

    except Exception as exc:
        # LLM 不可用时降级为基于规则的推断
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
                # recommendations: {"priority": 1, "action": "xxx"}
                elif "action" in item:
                    out.append(f"[P{item.get('priority', '?')}] {item['action']}")
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
