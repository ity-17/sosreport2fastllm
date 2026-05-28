# SOSReport RCA Engine

基于 AI 的 Linux sosreport 自动化根因分析工具。

上传一个 sosreport.tar.xz 压缩包，自动完成日志提取、时间线构建、异常事件检测和 LLM 根因推理，输出结构化的故障分析报告。

## 工作流程

```
sosreport.tar.xz
    │
    ▼ ① 解压 + 分类（过滤无用目录，保留日志 + SAR 指标）
    │
    ▼ ② 时间线构建（提取时间戳，写入 SQLite）
    │
    ▼ ③ 规则引擎（27 条关键词正则 + 10 条阈值规则 + 去重聚合）
    │
    ▼ ④ LLM 根因分析（DeepSeek API）
    │
    ▼ ⑤ 报告生成（Markdown / Web JSON）
    │
    ▼
  report.md / Web UI
```

## 快速开始

### 1. 环境要求

- Python 3.10+
- 依赖安装：`pip install -r requirements.txt`

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入你的 DeepSeek API Key
```

在 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys) 获取 API Key。

### 3. 命令行使用

```bash
python src/main.py sosreport.tar.xz \
    --fault-desc "15:32 系统卡死" \
    --fault-time "15:32" \
    --margin 15 \
    --output report.md
```

参数说明：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `sosreport` | sosreport 压缩包路径 | 必填 |
| `--fault-desc` | 故障描述 | 必填 |
| `--fault-time` | 故障时间点，如 `15:32` | 从故障描述中提取 |
| `--margin` | 时间窗口前后分钟数 | 15 |
| `--output` | 输出报告路径 | report.md |
| `--no-llm` | 跳过 LLM，仅输出规则引擎结果 | - |
| `--model` | LLM 模型 | deepseek-chat |

### 4. Web 界面使用

```bash
python src/web.py
# 浏览器打开 http://localhost:8080
```

拖拽上传 sosreport 文件，填写故障描述和时间，点击分析。

## 项目结构

```
pinganyunwei/
├── .env.example              # 配置模板
├── .env                      # 真实配置（gitignore）
├── requirements.txt          # Python 依赖
├── index.html                # Web 前端页面
├── src/
│   ├── config.py             # 统一配置读取
│   ├── models.py             # Pydantic 数据模型
│   ├── main.py               # CLI 入口
│   ├── web.py                # Web 服务入口
│   ├── extractor/            # ① 解压 + 文件分类
│   ├── timeline/             # ② 时间线构建（SQLite）
│   ├── rules/                # ③ 规则引擎（关键词 + 阈值 + 聚合）
│   ├── rca/                  # ④ LLM 根因分析
│   ├── report/               # ⑤ Markdown 报告生成
│   └── parsers/              # 日志/SAR/dmesg 解析器
├── evaluation/               # 评估框架
│   ├── evaluator.py          # 自动评估脚本
│   └── cases/                # 测试案例
└── scripts/                  # 辅助脚本
```
