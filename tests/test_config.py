"""AI 配置层测试（2026-08-20 拍板：本地不烧真实额度＋提供方可配置＋写死参数环境化）。

get_settings 会从项目根 .env 补缺（setdefault），本机 .env 有真实 key——所有用例一律把
ENV_PATH 指到不存在的文件隔离 .env，再逐个 setenv/delenv 钉死环境。
"""

import asyncio
import json

import httpx

import app.config as config
from app.ai import DeepSeekClient
from app.config import (
    DEFAULT_AI_BASE_URL,
    DEFAULT_AI_MAX_RETRIES,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_README_HEAD_CHARS,
    DEFAULT_AI_TIMEOUT_SECONDS,
    get_settings,
)

_AI_VARS = (
    "AI_ENABLED",
    "AI_API_KEY",
    "DEEPSEEK_API_KEY",
    "AI_BASE_URL",
    "AI_MODEL",
    "AI_TIMEOUT_SECONDS",
    "AI_MAX_RETRIES",
    "AI_README_HEAD_CHARS",
)


def _isolate(monkeypatch, tmp_path):
    """隔离真实 .env＋清空全部 AI 变量，返回干净基线。"""
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "nonexistent.env")
    for var in _AI_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_when_unset(monkeypatch, tmp_path):
    """零配置：提供方缺省 DeepSeek、AI 开启、key 空（走既有降级）、写死参数取缺省。"""
    _isolate(monkeypatch, tmp_path)
    s = get_settings()
    assert s.ai_enabled is True
    assert s.ai_api_key == ""
    assert s.ai_base_url == DEFAULT_AI_BASE_URL == "https://api.deepseek.com/v1/chat/completions"
    assert s.ai_model == DEFAULT_AI_MODEL == "deepseek-chat"
    assert s.ai_timeout_seconds == DEFAULT_AI_TIMEOUT_SECONDS == 60
    assert s.ai_max_retries == DEFAULT_AI_MAX_RETRIES == 1
    assert s.ai_readme_head_chars == DEFAULT_AI_README_HEAD_CHARS == 8000


def test_ai_api_key_preferred_over_legacy(monkeypatch, tmp_path):
    """AI_API_KEY 为现行键名，优先于兼容键 DEEPSEEK_API_KEY。"""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_API_KEY", "new-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "legacy-key")
    assert get_settings().ai_api_key == "new-key"


def test_legacy_deepseek_key_fallback(monkeypatch, tmp_path):
    """存量 .env 只有 DEEPSEEK_API_KEY 时回退读取（生产平滑过渡，不受改名影响）。"""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "legacy-key")
    assert get_settings().ai_api_key == "legacy-key"


def test_ai_disabled_forces_empty_key(monkeypatch, tmp_path):
    """AI_ENABLED=0：即使配了 key 也强制落空——全链路走既有"key 缺失"降级，零真实调用。"""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_API_KEY", "real-key")
    monkeypatch.setenv("AI_ENABLED", "0")
    s = get_settings()
    assert s.ai_enabled is False
    assert s.ai_api_key == ""


def test_provider_and_tunables_overridable(monkeypatch, tmp_path):
    """换提供方/调参数只改环境变量：base_url、model、超时、重试、README 截断全部生效。"""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_BASE_URL", "https://api.moonshot.cn/v1/chat/completions")
    monkeypatch.setenv("AI_MODEL", "kimi-k2")
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("AI_MAX_RETRIES", "3")
    monkeypatch.setenv("AI_README_HEAD_CHARS", "2000")
    s = get_settings()
    assert s.ai_base_url == "https://api.moonshot.cn/v1/chat/completions"
    assert s.ai_model == "kimi-k2"
    assert (s.ai_timeout_seconds, s.ai_max_retries, s.ai_readme_head_chars) == (30, 3, 2000)


def test_invalid_int_falls_back_to_default(monkeypatch, tmp_path):
    """整型变量写错（非数字/零/负数）自动回落缺省，不把服务起崩。"""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "abc")
    monkeypatch.setenv("AI_MAX_RETRIES", "0")
    monkeypatch.setenv("AI_README_HEAD_CHARS", "-100")
    s = get_settings()
    assert (s.ai_timeout_seconds, s.ai_max_retries, s.ai_readme_head_chars) == (60, 1, 8000)


def test_client_uses_custom_base_url_and_model():
    """DeepSeekClient 实例级 base_url/model：请求实际打到自定义端点、payload 用自定义模型。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.com/v1/chat/completions"
        assert json.loads(request.content)["model"] == "some-other-model"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = DeepSeekClient(
        "k",
        base_url="https://example.com/v1/chat/completions",
        model="some-other-model",
        transport=httpx.MockTransport(handler),
    )
    assert asyncio.run(client.translate("hello")) == "ok"
