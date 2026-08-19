"""测试全局收口：把整个测试套件与开发者本机真实 .env 隔离。

背景（2026-08-20）：本机 .env 有真实 DEEPSEEK_API_KEY，且本地为"不烧真实额度"追加了 AI_ENABLED=0——
get_settings 的 setdefault 补缺会把这些真实值带进测试进程，导致 AI 相关用例随本机 .env 内容波动
（今天 AI_ENABLED=0 让 35 个用例集体误判降级路径）。测试必须自含：每个用例需要的 env 由自己
monkeypatch.setenv 显式钉死，真实 .env 一律不可见。
"""

import pytest

import app.config as config


@pytest.fixture(autouse=True)
def _isolate_real_dotenv(monkeypatch, tmp_path):
    """双保险隔离：① .env 加载路径指到不存在文件（_load_dotenv 静默跳过）；
    ② 清掉进程级环境变量里的 RADAR 相关键——系统层 export 的同名变量也不许泄漏进测试进程。"""
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "nonexistent.env")
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")  # 测试进程不启真实调度（贴近本地开发姿态，评审观察项）
    for var in (
        "GITHUB_TOKEN",
        "RADAR_DB_PATH",
        "AI_ENABLED",
        "AI_API_KEY",
        "DEEPSEEK_API_KEY",
        "AI_BASE_URL",
        "AI_MODEL",
        "AI_TIMEOUT_SECONDS",
        "AI_MAX_RETRIES",
        "AI_README_HEAD_CHARS",
    ):
        monkeypatch.delenv(var, raising=False)
