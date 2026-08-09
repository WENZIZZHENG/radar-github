"""GitHub 客户端单测：全部走 MockTransport + 注入假 sleep，离线可跑，禁止真打 API。

不引 pytest-asyncio（不加新依赖）：异步用例统一 asyncio.run 驱动。
"""

import asyncio
import json
import os
import re
import time

import httpx
import pytest

from app.collector.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubRateLimitError,
    backoff_wait_seconds,
    rate_limit_wait_seconds,
    search_wait_seconds,
    utc_now_iso,
)
from app.config import _load_dotenv

TOKEN = "fake-token"  # 仅供请求头占位，MockTransport 下不会真发出去


def _make_client(handler, sleeps: list[float], **kwargs) -> GitHubClient:
    """构造离线客户端：MockTransport 拦截 HTTP，假 sleep 记录等待时长而不真等。"""

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return GitHubClient(TOKEN, transport=httpx.MockTransport(handler), sleep=fake_sleep, **kwargs)


# ---------- 纯函数：限速/退避时长计算 ----------


def test_search_wait_seconds():
    assert search_wait_seconds(None, 100.0, 2.1) == 0.0  # 首次调用不等
    assert search_wait_seconds(100.0, 101.0, 2.1) == pytest.approx(1.1)  # 补足间隔
    assert search_wait_seconds(100.0, 105.0, 2.1) == 0.0  # 已超过间隔


def test_rate_limit_wait_retry_after_priority():
    headers = httpx.Headers({"Retry-After": "5", "X-RateLimit-Reset": "9999999999"})
    assert rate_limit_wait_seconds(headers) == 6.0  # Retry-After 优先，+1 秒缓冲


def test_rate_limit_wait_from_reset_header():
    headers = httpx.Headers({"X-RateLimit-Reset": "1060"})
    assert rate_limit_wait_seconds(headers, now=1000.0) == 61.0
    assert rate_limit_wait_seconds(headers, now=2000.0) == 1.0  # reset 已过期：不睡负数，只留缓冲


def test_rate_limit_wait_fallback():
    assert rate_limit_wait_seconds(httpx.Headers()) == 60.0  # 两个头都没有：兜底 60 秒


def test_backoff_wait_seconds():
    assert backoff_wait_seconds(0) == 1.0
    assert backoff_wait_seconds(1) == 2.0
    assert backoff_wait_seconds(2) == 4.0
    assert backoff_wait_seconds(100) == 60.0  # 封顶


# ---------- 客户端行为：重试 / 限速 / 报错 ----------


def test_403_retry_after_then_success():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(403, headers={"Retry-After": "2"}, json={"message": "API rate limit exceeded"})
        return httpx.Response(200, json={"total_count": 1, "items": []})

    async def main() -> None:
        sleeps: list[float] = []
        async with _make_client(handler, sleeps) as client:
            data = await client.search_repositories("stars:>50000")
        assert data["total_count"] == 1
        assert len(calls) == 2
        assert sleeps == [3.0]  # Retry-After 2 秒 + 1 秒缓冲

    asyncio.run(main())


def test_429_retry_with_reset_header():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            reset = str(int(time.time()) + 5)
            return httpx.Response(429, headers={"X-RateLimit-Reset": reset}, json={"message": "too many requests"})
        return httpx.Response(200, json={"total_count": 0, "items": []})

    async def main() -> None:
        sleeps: list[float] = []
        async with _make_client(handler, sleeps) as client:
            await client.search_repositories("stars:>50000")
        assert len(calls) == 2
        assert len(sleeps) == 1
        assert 5.0 < sleeps[0] <= 6.0  # 到 reset 的剩余秒数 +1 秒缓冲

    asyncio.run(main())


def test_rate_limit_retry_exhausted():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, headers={"Retry-After": "0"}, json={"message": "rate limit"})

    async def main() -> None:
        sleeps: list[float] = []
        async with _make_client(handler, sleeps, max_retries=2) as client:
            with pytest.raises(GitHubRateLimitError, match="限速"):
                await client.fetch_repos_by_ids(["node-1"])
        assert len(calls) == 3  # 首次 + 2 次重试
        assert sleeps == [1.0, 1.0]  # 每次失败后按 Retry-After 等待（0 秒 +1 秒缓冲）

    asyncio.run(main())


def test_5xx_exponential_backoff():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        status = 500 if len(calls) <= 2 else 200
        return httpx.Response(status, json={"data": {"nodes": []}})

    async def main() -> None:
        sleeps: list[float] = []
        async with _make_client(handler, sleeps) as client:
            nodes = await client.fetch_repos_by_ids(["node-1"])
        assert nodes == []
        assert len(calls) == 3
        assert sleeps == [1.0, 2.0]  # 指数退避：第 1/2 次失败后分别等 1 秒、2 秒

    asyncio.run(main())


def test_5xx_retry_exhausted():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"message": "boom"})

    async def main() -> None:
        sleeps: list[float] = []
        async with _make_client(handler, sleeps, max_retries=2) as client:
            with pytest.raises(GitHubError, match="服务端错误"):
                await client.fetch_repos_by_ids(["node-1"])
        assert len(calls) == 3  # 首次 + 2 次重试
        assert sleeps == [1.0, 2.0]

    asyncio.run(main())


def test_401_raises_clear_auth_error():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"message": "Bad credentials"})

    async def main() -> None:
        sleeps: list[float] = []
        async with _make_client(handler, sleeps) as client:
            with pytest.raises(GitHubAuthError, match="GitHub token 无效或已过期"):
                await client.fetch_repos_by_ids(["node-1"])
        assert len(calls) == 1  # 401 不重试
        assert sleeps == []

    asyncio.run(main())


def test_empty_token_rejected_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("空 token 不得发出任何 HTTP 请求")

    async def main() -> None:
        client = GitHubClient("", transport=httpx.MockTransport(handler))
        async with client:
            with pytest.raises(GitHubAuthError, match="GitHub token 为空"):
                await client.search_repositories("stars:>50000")

    asyncio.run(main())


def test_search_pacing_enforces_min_interval():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_count": 0, "items": []})

    async def main() -> None:
        sleeps: list[float] = []
        async with _make_client(handler, sleeps, min_search_interval=2.1) as client:
            await client.search_repositories("q1")
            await client.search_repositories("q2")
        # 首次不限速；第二次补足 2.1 秒间隔（假 sleep 不耗真实时间，故接近满额）
        assert len(sleeps) == 1
        assert 2.0 < sleeps[0] <= 2.1

    asyncio.run(main())


def test_fetch_repos_over_100_ids_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("超过 100 个 id 不得发出请求")

    async def main() -> None:
        async with _make_client(handler, []) as client:
            with pytest.raises(ValueError, match="最多 100"):
                await client.fetch_repos_by_ids([f"node-{i}" for i in range(101)])

    asyncio.run(main())


def test_fetch_repos_parses_nodes_and_preserves_null():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        nodes = [{"id": "A", "nameWithOwner": "octocat/hello", "stargazerCount": 42}, None]
        return httpx.Response(200, json={"data": {"nodes": nodes}})

    async def main() -> None:
        async with _make_client(handler, []) as client:
            nodes = await client.fetch_repos_by_ids(["A", "B"])
        assert nodes[0]["stargazerCount"] == 42
        assert nodes[1] is None  # null 是仓库删除/转私的死库信号，必须原样透传给调用方（T-005 置 dead）
        # 请求体确为 nodes(ids:) 批量形态，variables 透传全部 id
        assert "nodes(ids:" in captured["query"].replace(" ", "")
        assert captured["variables"] == {"ids": ["A", "B"]}

    asyncio.run(main())


def test_graphql_error_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "Could not resolve to a node"}]})

    async def main() -> None:
        async with _make_client(handler, []) as client:
            with pytest.raises(GitHubError, match="GraphQL"):
                await client.fetch_repos_by_ids(["bad-id"])

    asyncio.run(main())


# ---------- config.py 的 .env 加载 ----------


def test_dotenv_system_env_priority(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("RADAR_TEST_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("RADAR_TEST_KEY", "from-system")
    _load_dotenv(env_file)
    assert os.environ["RADAR_TEST_KEY"] == "from-system"  # 系统环境变量优先，不覆盖


def test_dotenv_parses_key_value_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("RADAR_TEST_KEY", raising=False)
    monkeypatch.delenv("RADAR_TEST_EQ", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("# 注释行\n\nRADAR_TEST_KEY=abc\nRADAR_TEST_EQ=a=b=c\n   \n", encoding="utf-8")
    _load_dotenv(env_file)
    assert os.environ["RADAR_TEST_KEY"] == "abc"
    assert os.environ["RADAR_TEST_EQ"] == "a=b=c"  # split("=", 1)：值里的等号原样保留


def test_dotenv_missing_file_silent(tmp_path):
    _load_dotenv(tmp_path / "不存在的文件.env")  # 静默跳过：不抛异常即通过



# ---------- 评审 findings 修复锁定 ----------


def test_utc_now_iso_format():
    """UTC 定长硬约定（schema.sql:4）：改成带微秒/时区偏移的变体会让字典序比较静默错乱，格式必须锁死。"""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", utc_now_iso())


def test_403_without_rate_limit_headers_fails_fast():
    """非限速 403（无限速头）：直接报错，不重试不白等兜底 60 秒。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    async def main() -> None:
        sleeps: list[float] = []
        async with _make_client(handler, sleeps, max_retries=2) as client:
            with pytest.raises(GitHubError, match="未带限速头"):
                await client.fetch_repos_by_ids(["node-1"])
        assert len(calls) == 1
        assert sleeps == []

    asyncio.run(main())



def test_dotenv_utf8_bom_tolerated(tmp_path, monkeypatch):
    """BOM 兼容：编辑器存出的 UTF-8 BOM 不得污染首行键名（utf-8-sig 解码）。"""
    monkeypatch.delenv("RADAR_TEST_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xef\xbb\xbfRADAR_TEST_KEY=abc\n")
    _load_dotenv(env_file)
    assert os.environ["RADAR_TEST_KEY"] == "abc"  # 键名必须干净，无 ﻿ 前缀


def test_dotenv_gbk_raises_clear_error(tmp_path):
    """GBK/ANSI 保存的 .env：报错必须指向文件与编码，不许静默跳过。"""
    env_file = tmp_path / ".env"
    env_file.write_bytes("RADAR_TEST_KEY=中文\n".encode("gbk"))
    with pytest.raises(ValueError, match="UTF-8"):
        _load_dotenv(env_file)
