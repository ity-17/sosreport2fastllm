"""统一配置 — 所有环境变量集中管理，支持 .env 文件加载."""
import os
from pathlib import Path

# 尝试加载项目根目录的 .env 文件
_ENV_PATH = Path(__file__).parent.parent / ".env"
if _ENV_PATH.exists():
    with open(_ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

# LLM 配置
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-reasoner")

# Web 服务配置
WEB_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))

# 默认时间窗口（分钟）
DEFAULT_MARGIN = int(os.environ.get("DEFAULT_MARGIN", "15"))
