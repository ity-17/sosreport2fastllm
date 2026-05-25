# SOSReport RCA Engine V0.1 — 项目全景文档

## 1. 项目概述

**目标**：Linux 运维 AI 诊断系统。输入 sosreport 诊断包（tar.xz），自动提取日志、解析 SAR 性能指标、通过规则引擎检测异常事件、调用 LLM 推理根因，输出 Markdown 诊断报告。

**适用场景**：Kubernetes 集群节点故障、存储 I/O Hang、OOM Kill、CPU 软死锁、文件系统损坏等 Linux 内核级故障的回溯分析。

**用户**：Linux 运维工程师，日常处理线上 RHEL/Kylin 系统的 sosreport 诊断包。

---

## 2. 技术栈

| 层 | 技术 | 用途 |
|---|---|---|
| 语言 | Python 3.11 | 全部后端 |
| 数据模型 | Pydantic v2 | 模块间数据结构（FileEntry、RuleEvent、RCAOutput 等） |
| Web 框架 | Flask | Web UI + REST API |
| Web 前端 | 原生 HTML/CSS/JS | 单页应用，文件上传 + 报告渲染 |
| 数据库 | SQLite（WAL 模式） | 时间线事件存储 + SAR 指标查询 |
| XML 解析 | xml.etree.ElementTree | SAR XML 文件解析（sysstat DTD 格式） |
| LLM | DeepSeek API（OpenAI 兼容） | 根因分析推理，模型：deepseek-chat |
| SSH | paramiko | VM 远程自动化（测试数据生成） |
| 测试环境 | Rocky Linux 9.7（VMware） | 真实 RHEL 系环境，sysstat+sos+stress-ng |
| 打包格式 | tarfile | sosreport tar.xz 解压 |

---

## 3. 项目目录结构

```
pinganyunwei/
├── 启动分析工具.bat              # Windows 一键启动脚本
├── requirements.txt              # Python 依赖
├── PROJECT_OVERVIEW.md           # 本文档
├── index.html                    # Web 前端（文件上传 + 报告展示）
├── src/
│   ├── main.py                   # CLI 主入口
│   ├── web.py                    # Flask Web 服务入口
│   ├── models.py                 # Pydantic 数据模型（全局共享）
│   ├── extractor/
│   │   └── __init__.py           # tar.xz 解压 + 文件分类索引
│   ├── timeline/
│   │   └── __init__.py           # 时间引擎：日志行→SQLite、时间窗口查询
│   ├── parsers/
│   │   ├── syslog.py             # syslog 时间解析（RHEL mm/dd hh:mm:ss 格式）
│   │   ├── dmesg.py              # dmesg 启动时间推断 + 相对时间解析
│   │   └── sar.py                # SAR 解析（XML 优先 → 二进制 sadf 回退）
│   ├── rules/
│   │   ├── __init__.py           # 规则引擎总调度 + 数据质量评估
│   │   ├── keyword_rules.py      # 关键词正则规则（27 条，匹配内核异常行）
│   │   └── threshold_rules.py    # 阈值规则（SAR 指标阈值判断）
│   ├── rca/
│   │   └── __init__.py           # LLM RCA Agent（唯一 LLM 调用点）
│   └── report/
│       └── __init__.py           # Markdown 报告生成器
├── scripts/
│   ├── sadf_json_to_xml.py       # sadf JSON → 解析器兼容 XML（Ubuntu 用）
│   ├── generate_real_scenarios.py # 自动化场景生成（SSH 到 VM 执行）
│   └── generate_mock_sosreport.py # 虚拟测试数据生成器
└── test_scenarios/               # 4 个 RHEL 格式真实测试场景（tar.xz）
    ├── io_hang_oom_rocky.tar.xz
    ├── memory_exhaustion_rocky.tar.xz
    ├── cpu_softlockup_rocky.tar.xz
    └── filesystem_corruption_rocky.tar.xz
```

---

## 4. 五步 Pipeline 架构

```
[tar.xz] → Extractor → [FileManifest] → Timeline Engine → [SQLite db]
                                                                    ↓
[report.md] ← Report Generator ← [RCAOutput] ← RCA LLM Agent ← [StructuredEvidence]
                                                                   ↑
                                              Rule Engine (Keyword + Threshold)
```

### Step 1: Extractor（文件提取 + 分类）

- 流式解压 tar.xz，跳过无用目录（proc/、sys/、sos_reports/、sos_strings/ 等）
- 文件类型识别（优先级匹配）：
  - `sos_commands/sar/saXX.xml` → SAR_XML
  - `var/log/sa/saXX` → SAR_BINARY
  - `var/log/messages` → SYSLOG
  - `var/log/containers/*.log` → CONTAINER_LOG
  - `sos_commands/kernel/dmesg` → DMESG
  - `var/log/journal/*.journal` → JOURNAL
- 输出：`FileManifest`（含文件路径、类型、大小、优先级）

### Step 2: Timeline Engine（时间线构建）

- 读取 syslog/dmesg/容器日志，逐行提取 UNIX 时间戳
- 无时间戳的行标记 `ts=-1`（不会被关键词规则检索，但保留在 DB 中）
- dmesg：通过 `/proc/stat` 的 `btime` 字段获取启动时间，将相对秒数转为绝对时间戳
- 批量写入 SQLite（每 5000 行一个 batch），100K+ 行秒级完成
- SAR 数据单独写入 `metrics` 表（generator 流式写入，不占用内存）
- 根据用户指定的故障时间 `HH:MM` 计算时间窗口（默认 ±15 分钟）

### Step 3: Rule Engine（规则引擎）

- **关键词规则**（27 条正则）：在时间窗口内的事件行中搜索模式
  - 内存：OOM Kill、Cgroup OOM、进程被杀
  - 内核：Blocked Task（D 状态进程）、RCU Stall、Soft Lockup、Kernel Panic
  - 存储：I/O Error、EXT4/XFS 错误、文件系统只读、Multipath 故障
  - 网络：SYN Flood、Conntrack 满
  - systemd：服务失败、服务被杀
- **阈值规则**（10 条）：从 `metrics` 表中查询聚合值（MAX/AVG/MIN），与阈值比较
  - CPU：iowait >50%（高）、>80%（极端）、steal >30%
  - 内存：memused >90%、swapused >50%、memfree <5%
  - 磁盘：await >5000ms（严重）、>1000ms（高）、util >90%
- **去重**：相同 (event_type, source_file, timestamp) 只记录一次
- **质量评估**：检查 syslog/joural/dmesg/sar 4 个数据源的有无
  - 4 源中 >=3 个有数据 → HIGH
  - >=2 个 → MEDIUM
  - <2 个 → LOW

### Step 4: RCA LLM Agent

- 将 `StructuredEvidence`（事件列表 + 严重度统计 + 时间线）构建为结构化的 System Prompt
- 调用 DeepSeek API 进行根因推理
- 输出 `RCAOutput`：根因、摘要、时间线、证据链、影响范围、竞争性假设、修复建议
- 降级方案：LLM 不可用时基于规则组合推断（如 I/O Error + Blocked Task → 存储链路故障）

### Step 5: Report Generator

- 生成 Markdown 格式诊断报告
- 包含：分析时间、故障时间、数据质量、摘要、根因、影响、时间线、证据、竞争性假设、修复建议

---

## 5. 数据模型（Pydantic）

```python
class FileEntry(BaseModel):
    path: str              # 相对路径，如 "var/log/messages"
    abs_path: str          # 磁盘绝对路径
    log_type: LogType      # SYSLOG | SAR_XML | SAR_BINARY | DMESG | etc.
    size_bytes: int
    priority: int = 1      # 3=核心日志, 2=一般日志, 1=配置文件

class FileManifest(BaseModel):
    total_files: int
    entries: list[FileEntry]

class RuleEvent(BaseModel):
    event_type: str        # 如 "OOM_KILL", "BLOCKED_TASK"
    timestamp: int | None
    severity: str          # CRITICAL | HIGH | MEDIUM | LOW
    source_file: str
    evidence: dict         # {"raw_line": "..."} 或 {"metric": "cpu_iowait", "value": 75.2}

class StructuredEvidence(BaseModel):
    fault_description: str
    fault_time: str
    time_window: tuple[int, int]
    events: list[RuleEvent]
    data_quality: str      # HIGH | MEDIUM | LOW

class RCAOutput(BaseModel):
    root_cause: str
    summary: str
    timeline: list[str]
    evidence: list[str]
    impact: str
    alternative_hypotheses: list[str]
    recommendations: list[str]
```

---

## 6. SAR 数据处理

### 数据源优先级
1. `sos_commands/sar/saXX.xml` — RHEL 的 sosreport 自动通过 `sadf -x` 生成的 XML
2. `var/log/sa/saXX` — 二进制格式，需通过 `sadf -j` 解析（需本地安装 sysstat）

### XML 解析格式
标准 sysstat DTD XML，namespace `http://pagesperso-orange.fr/sebastien.godard/sysstat`：
```xml
<timestamp date="2026-05-25" time="19:16:01" utc="1" interval="61">
  <cpu-load>
    <cpu number="all" user="12.22" nice="0.00" system="3.55" iowait="0.03" steal="0.00" idle="84.20"/>
  </cpu-load>
  <memory memfree="12345678" memused="4567890"/>
  <disk device="sda" await="2.34" util="45.67" tps="123.4"/>
  <net-dev iface="ens33" rxkB="1234" txkB="5678"/>
  <load-average load1="0.50" load5="0.75" load15="0.90"/>
</timestamp>
```
- UTC 标志为 `"1"` 时会在解析时加 8 小时偏移（Asia/Shanghai）
- CPU 属性名映射：RHEL 的 `sadf -x` 使用 `user`/`system`/`iowait` 等标准名称，与解析器一致

### Generator 流式解析
```python
def parse_sar_files(sos_root: str) -> Iterator[tuple[int, str, float, str]]:
    # yield (timestamp, metric_name, value, hostname)
    # 不实例化大列表，直接喂入 SQLite executemany
```

---

## 7. Web 界面

### 前端（index.html）
- 单页应用，支持拖拽上传或文件选择
- 表单字段：故障描述、故障时间（HH:MM）、时间窗口（分钟）、LLM 模型选择
- 结果展示区：数据质量徽章、异常事件统计、根因结论、时间线、处理建议
- 每个处理步骤显示耗时

### 后端 API
```
POST /api/analyze
  multipart/form-data:
    file: sosreport tar.xz
    fault_desc: "IO hang 导致 MySQL 不可用"
    fault_time: "15:32"
    margin: 15
    model: "deepseek-chat"
```
响应 JSON：
```json
{
  "success": true,
  "report": {
    "data_quality": "HIGH",
    "abnormal_events": 24,
    "root_cause": "...",
    "timeline": [...],
    "recommendations": [...]
  },
  "steps": [{"step": "解压+索引", "time": 2.1, "detail": "发现 1023 个文件"}, ...],
  "total_time": 14.2
}
```

---

## 8. 测试数据

### 当前测试集
4 个在 Rocky Linux 9.7 上通过真实 stress-ng 压力 + syslog 异常注入生成的场景：

| 场景 | 文件 | 负载 | 注入异常 |
|---|---|---|---|
| IO Hang + OOM | io_hang_oom_rocky.tar.xz | stress-ng --hdd 2 --io 2 | I/O 错误、Blocked Task、OOM Kill、EXT4 错误 |
| 内存耗尽 | memory_exhaustion_rocky.tar.xz | stress-ng --vm 2 --vm-bytes 80% | OOM Killer、Blocked Task、Cgroup OOM |
| CPU 软死锁 | cpu_softlockup_rocky.tar.xz | stress-ng --cpu 4 | Soft Lockup、RCU Stall、HUNG_TASK |
| 文件系统损坏 | filesystem_corruption_rocky.tar.xz | stress-ng --hdd 2 --io 2 | I/O Error、EXT4 Error、FS Remount RO |

每个场景：
- 数据质量：HIGH
- 异常事件：22-31 个
- SAR 指标：495-1,683 条
- 文件数：~1023 个
- 压缩包大小：~10MB

### 生成方式
通过 `scripts/run_scenario.sh`（在 VM 上执行）：
1. SAR 基线采集（2 次 sa1）
2. `logger -p kern.crit "异常消息"` 注入 anomalies（匹配关键词规则）
3. stress-ng 后台运行（3 分钟）
4. 压力期间采集 SAR（3 次 sa1）
5. sosreport --batch 生成 tar.xz
6. 下载到 Windows

---

## 9. 已知问题与改进方向

### 已知限制
1. **阈值规则触发率低**：SAR XML 中 memory/disk/load 指标可能未全部解析（当前仅 CPU 和网络数据完整）
2. **时间戳无年份**：RHEL syslog 格式 `May 11 03:19:01` 不含年份，默认用当前年份
3. **dmesg 启动时间依赖**：需要 `/proc/stat` 的 `btime` 字段，部分 sosreport 可能缺失
4. **无语义关联**：关键词规则是纯正则匹配，不知道 "OOM Kill mysql" 和 "MySQL 慢查询变多" 之间的因果关联
5. **单时间窗口**：只分析一个故障时间窗口，无法处理跨多个时间段的级联故障
6. **Web 无认证**：本地单用户模式，不适合多用户部署
7. **并发处理**：无队列机制，同时上传多个文件会导致阻塞

### 可探索方向
1. 增加更多 SAR 指标类型（内存、磁盘 I/O 等待时间、网络错误率）
2. 关键词规则加入上下文关联（同一进程的多个异常事件串联）
3. 支持 journald 日志格式（当前跳过了 `.journal` 二进制文件）
4. LLM 调用加入 RAG（检索知识库中的历史故障案例）
5. 前端时间线可视化（ECharts 渲染 SAR 指标曲线 + 异常事件标记）
6. Docker 化部署
7. 支持多时间窗口分析（级联故障场景）

---

## 10. 开发环境

### Windows（分析端）
- Python 3.11 + pip
- 依赖：pydantic、openai、flask、paramiko、pyyaml
- 启动：双击 `启动分析工具.bat` 或 `python src/main.py`
- 端口：8080（Web UI）

### Rocky Linux 9.7（数据生成端，VMware VM）
- 4 vCPU / 4GB RAM / 30GB 磁盘
- 安装包：sysstat、sos、stress-ng、tar
- SAR 采集：cron 每 2 分钟（生产建议 10 分钟）
- 网络：NAT，IP 192.168.234.129
- SSH：root@192.168.234.129
