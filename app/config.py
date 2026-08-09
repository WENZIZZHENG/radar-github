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


@dataclass(frozen=True)
class Settings:
    github_token: str
    deepseek_api_key: str
    db_path: Path


def get_settings() -> Settings:
    _load_dotenv(ENV_PATH)  # 幂等：setdefault 不覆盖既有变量，重复调用无副作用
    return Settings(
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        db_path=Path(os.environ.get("RADAR_DB_PATH", str(DEFAULT_DB_PATH))),
    )
