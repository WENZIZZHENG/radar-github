"""运行配置：全部从环境变量读取，密钥不落库、不落仓（S 档发布就绪：密钥不入库）。

token 缺失时不在这里报错——骨架阶段服务本身不依赖外部 API；
由真正使用它的模块（采集 T-003 / AI T-011）在使用点校验并给出清晰报错。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "radar.db"


@dataclass(frozen=True)
class Settings:
    github_token: str
    deepseek_api_key: str
    db_path: Path


def get_settings() -> Settings:
    return Settings(
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        db_path=Path(os.environ.get("RADAR_DB_PATH", str(DEFAULT_DB_PATH))),
    )
