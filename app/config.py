"""运行配置：全部从环境变量读取，密钥不落库、不落仓（S 档发布就绪：密钥不入库）。

token 缺失时不在这里报错——榜单服务本身不依赖外部 API；
由真正使用它的模块（采集 T-003 / AI T-011）在使用点校验并给出清晰报错。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "radar.db"
ENV_PATH = BASE_DIR / ".env"

# AI 提供方缺省值（DeepSeek；OpenAI 兼容 chat/completions 协议，换提供方只改 .env 变量）
DEFAULT_AI_BASE_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_AI_MODEL = "deepseek-chat"
DEFAULT_AI_TIMEOUT_SECONDS = 60  # LLM 响应慢于普通 REST，缺省放宽到 60 秒
DEFAULT_AI_MAX_RETRIES = 1  # 任务书口径：重试一次后仍失败 → 抛清晰异常
DEFAULT_AI_README_HEAD_CHARS = 8000  # README 截断入 prompt 上限（≈2000~3000 tokens，单次调用成本主杠杆）


def _load_dotenv(path: Path) -> None:
    """自解析 KEY=VALUE 行注入环境变量（不引 python-dotenv：需要的格式规则只有三行）。

    系统环境变量优先——已存在的键一律不覆盖（setdefault），服务器/CI 直接注入的变量永远生效；
    .env 不存在时静默跳过，本机开发之外的部署形态不依赖该文件。
    """
    try:
        # utf-8-sig：兼容编辑器存出的 UTF-8 BOM——否则首行键名静默变成 \ufeffGITHUB_TOKEN，token 为空且根因难查
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return
    except UnicodeDecodeError as exc:
        # 中文 Windows 记事本默认 ANSI/GBK 保存会触发：报错必须指向 .env 编码，不在远处炸
        raise ValueError(f"{path} 不是有效 UTF-8：请用 UTF-8 编码重新保存 .env（{exc}）") from exc
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)  # 只切第一刀：值里允许含 =（如 base64 类密钥）
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip())


def _env_int(name: str, default: int) -> int:
    """整型环境变量：缺失/非法（非数字、≤0）一律回落缺省——配置写错不该把服务起崩。"""
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class Settings:
    github_token: str
    ai_api_key: str  # AI_ENABLED=0（本地开发不烧真实额度）时强制为空，全链路走既有"key 缺失"降级
    ai_enabled: bool
    ai_base_url: str
    ai_model: str
    ai_timeout_seconds: int
    ai_max_retries: int
    ai_readme_head_chars: int
    db_path: Path


def get_settings() -> Settings:
    _load_dotenv(ENV_PATH)  # 幂等：setdefault 不覆盖既有变量，重复调用无副作用
    ai_enabled = os.environ.get("AI_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
    # AI_API_KEY 为现行键名；DEEPSEEK_API_KEY 为兼容回退（存量 .env 平滑过渡，生产不受影响）
    api_key = os.environ.get("AI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
    return Settings(
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        ai_api_key=api_key if ai_enabled else "",
        ai_enabled=ai_enabled,
        ai_base_url=os.environ.get("AI_BASE_URL", DEFAULT_AI_BASE_URL),
        ai_model=os.environ.get("AI_MODEL", DEFAULT_AI_MODEL),
        ai_timeout_seconds=_env_int("AI_TIMEOUT_SECONDS", DEFAULT_AI_TIMEOUT_SECONDS),
        ai_max_retries=_env_int("AI_MAX_RETRIES", DEFAULT_AI_MAX_RETRIES),
        ai_readme_head_chars=_env_int("AI_README_HEAD_CHARS", DEFAULT_AI_README_HEAD_CHARS),
        db_path=Path(os.environ.get("RADAR_DB_PATH", str(DEFAULT_DB_PATH))),
    )
