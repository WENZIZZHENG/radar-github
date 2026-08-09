"""GitHub API 客户端（数据源决策见《架构决策记录》§3 决策 1）。

- GraphQL `nodes(ids:)` 批量为主：一次 ≤100 个 node id，初始/每日采集要用的字段一次拿全（T-005 直接复用）；
- Search REST 仅用于发现/初始分片：认证用户限速 30 次/分钟，客户端主动限速（连续调用间隔 ≥2.1 秒）；
- 限速重试：403/429 读 Retry-After 或 X-RateLimit-Reset 等待后重试，5xx 指数退避，均有重试上限；
  等待时长计算全部抽成纯函数（search_wait_seconds / rate_limit_wait_seconds / backoff_wait_seconds），
  sleep 与 transport 可注入，单测离线可跑、不真打 API；
- token 在使用点校验：空 token 不发请求直接报错；401 转成"token 无效或已过期"的清晰异常，不裸透传。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx

from app.config import BASE_DIR, get_settings

GRAPHQL_URL = "https://api.github.com/graphql"
SEARCH_URL = "https://api.github.com/search/repositories"
REPOS_URL = "https://api.github.com/repos"  # 单仓库元数据：/{owner}/{repo}（关注动态入池用）

MAX_NODES_PER_QUERY = 100  # nodes(ids:) 一次最多 100 个：GraphQL 单请求复杂度上限内最经济的批量
DEFAULT_MIN_SEARCH_INTERVAL = 2.1  # 秒；Search 限速 30 次/分钟 → 间隔须 ≥2.0，多留 0.1 秒防边界抖动
DEFAULT_MAX_RETRIES = 5
RATE_LIMIT_BUFFER_SECONDS = 1.0  # 在 reset/Retry-After 基础上多等 1 秒：卡点醒来会被再拒一次
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 60.0  # 两个响应头都缺失时的兜底等待
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0

# 字段一次拿全：nameWithOwner/description/primaryLanguage/topics 入库；
# isPrivate/isDisabled（及 nodes 返回 null）是死库信号（置 repos.dead 后停采）；
# isArchived 不算死库——归档只读但仍可被 star，星数仍会动，停采反丢数据（T-006 评审确认口径）；pushedAt 辅助判断停更。
NODES_QUERY = """
query BatchRepos($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Repository {
      id
      nameWithOwner
      description
      url
      stargazerCount
      forkCount
      primaryLanguage { name }
      repositoryTopics(first: 20) { nodes { topic { name } } }
      isArchived
      isPrivate
      isDisabled
      isFork
      createdAt
      updatedAt
      pushedAt
    }
  }
}
"""


class GitHubError(RuntimeError):
    """GitHub 调用失败的基类。"""


class GitHubAuthError(GitHubError):
    """token 缺失/无效：必须给出可操作的修复指引，不许裸 401 透传。"""


class GitHubRateLimitError(GitHubError):
    """重试上限耗尽后仍被限速。"""


class GitHubNotFoundError(GitHubError):
    """404：资源不存在/已删除/转私有。单独成类供调用方映射用户可见 404（关注未入池仓库）；其余路径语义不变。"""


def utc_now_iso() -> str:
    """UTC 定长时间戳（schema 硬约定 YYYY-MM-DDTHH:MM:SSZ）：字典序即时间序，入库与留痕统一用它。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def search_wait_seconds(last_call_at: float | None, now: float, min_interval: float) -> float:
    """距上次 Search 调用还需等多久才满足主动限速；首次调用不等。"""
    if last_call_at is None:
        return 0.0
    return max(0.0, min_interval - (now - last_call_at))


def rate_limit_wait_seconds(headers: httpx.Headers, *, now: float | None = None) -> float:
    """从 403/429 响应头算等待秒数：Retry-After 优先，其次 X-RateLimit-Reset（epoch 秒），都没有走兜底。"""
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after)) + RATE_LIMIT_BUFFER_SECONDS
        except ValueError:
            pass  # 规范允许 HTTP 日期格式，GitHub 实际只发秒数；解析失败落下一个头
    reset = headers.get("X-RateLimit-Reset")
    if reset is not None:
        try:
            current = time.time() if now is None else now
            return max(0.0, float(reset) - current) + RATE_LIMIT_BUFFER_SECONDS
        except ValueError:
            pass
    return DEFAULT_RATE_LIMIT_WAIT_SECONDS


def backoff_wait_seconds(attempt: int) -> float:
    """5xx 指数退避：1、2、4、8…秒，封顶 60 秒（attempt 为已失败次数，从 0 起）。"""
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempt)


class GitHubClient:
    """httpx AsyncClient 封装；transport / sleep 可注入（单测用 MockTransport + 假 sleep 离线跑）。"""

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        min_search_interval: float = DEFAULT_MIN_SEARCH_INTERVAL,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._token = token
        self._sleep = sleep
        self._min_search_interval = min_search_interval
        self._max_retries = max_retries
        self._last_search_at: float | None = None
        self._client = httpx.AsyncClient(
            # GitHub 强制要求 User-Agent，缺失直接 403；Bearer 认证 + 统一 API 版本头
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "radar-github",
            },
            transport=transport,
            timeout=30.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def search_repositories(self, query: str, *, sort: str = "stars", per_page: int = 100, page: int = 1) -> dict:
        """Search REST（仅发现/初始分片用）：主动限速后请求，返回原始 JSON。

        GitHub Search 只吐前 1000 条结果（per_page=100 时 page ≤ 10）；分片保证不越界是调用方（T-005）的责任。
        sort 默认 stars（初始分片按星数定序）；T-006 发现池传 updated 捞"最近活跃"新面孔——
        星数榜头部常年固化，按 updated 排序才能轮到涨星中的新仓库翻进前 500。
        """
        await self._pace_search()
        response = await self._request(
            "GET",
            SEARCH_URL,
            params={"q": query, "sort": sort, "order": "desc", "per_page": per_page, "page": page},
        )
        return response.json()

    async def fetch_repos_by_ids(self, ids: list[str]) -> list[dict | None]:
        """GraphQL nodes(ids:) 批量拉取；分片（>100）归调用方。nodes 里的 null 是仓库删除/转私的死库信号，原样透传。"""
        ids = list(ids)
        if not ids:
            return []
        if len(ids) > MAX_NODES_PER_QUERY:
            raise ValueError(f"nodes(ids:) 一次最多 {MAX_NODES_PER_QUERY} 个，收到 {len(ids)} 个；分片归调用方（T-005）")
        response = await self._request(
            "POST", GRAPHQL_URL, json={"query": NODES_QUERY, "variables": {"ids": ids}}
        )
        payload = response.json()
        errors = payload.get("errors")
        if errors:
            detail = "; ".join(str(e.get("message", e)) for e in errors)
            raise GitHubError(f"GitHub GraphQL 返回错误：{detail}")
        return payload["data"]["nodes"]

    async def fetch_repo(self, full_name: str) -> dict:
        """单仓库元数据（决策 9 关注动态入池）：REST GET /repos/{owner}/{repo}。

        一次请求拿全入库字段（full_name/node_id/description/language/topics/stargazers_count），
        响应字段与 Search item 同构，入库映射与采集层共用同一套口径。
        404（仓库不存在/已删除/转私有）抛 GitHubNotFoundError，由调用方映射用户可见错误；
        核心 REST 限速 5000 次/小时（认证），单发请求不配主动限速（Search 的 30 次/分钟才需要 _pace_search）。
        """
        response = await self._request("GET", f"{REPOS_URL}/{full_name}")
        return response.json()

    async def _pace_search(self) -> None:
        now = time.monotonic()
        wait = search_wait_seconds(self._last_search_at, now, self._min_search_interval)
        if wait > 0:
            await self._sleep(wait)
        # 间隔从"上一次发起请求的时刻"起算：限速按请求计数，这样取最保守口径
        self._last_search_at = time.monotonic()

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        # token 在使用点校验：config 层不报错（榜单页面不需要 token），这里第一刀拦住空 token
        if not self._token:
            raise GitHubAuthError("GitHub token 为空：请在项目根 .env 配置 GITHUB_TOKEN，或注入同名系统环境变量")
        for attempt in range(self._max_retries + 1):
            response = await self._client.request(method, url, **kwargs)
            status = response.status_code
            if status == 401:
                raise GitHubAuthError("GitHub token 无效或已过期：请更新项目根 .env 中的 GITHUB_TOKEN 后重试")
            if status in (403, 429):
                headers = response.headers
                if "Retry-After" not in headers and "X-RateLimit-Reset" not in headers:
                    # 非限速 403（资源禁止访问/abuse 检测等）：无限速头可等，按普通 4xx 直接报错——
                    # 否则会走兜底 60 秒×重试上限，白等 5 分钟后还报一个指向错误根因的"限速"
                    raise GitHubError(f"GitHub 请求被拒绝（HTTP {status}，响应未带限速头）：{response.text[:200]}")
                if attempt >= self._max_retries:
                    raise GitHubRateLimitError(f"GitHub 限速：重试 {self._max_retries} 次后仍被拒绝（HTTP {status}）")
                await self._sleep(rate_limit_wait_seconds(headers))
                continue
            if status >= 500:
                if attempt >= self._max_retries:
                    raise GitHubError(f"GitHub 服务端错误（HTTP {status}）：重试 {self._max_retries} 次后放弃")
                await self._sleep(backoff_wait_seconds(attempt))
                continue
            if status == 404:
                raise GitHubNotFoundError(f"GitHub 资源不存在（HTTP 404）：{response.text[:200]}")
            if status >= 400:
                raise GitHubError(f"GitHub 请求失败（HTTP {status}）：{response.text[:200]}")
            return response
        raise GitHubError("不可达：重试循环异常退出")  # 防御：max_retries≥0 时循环至少执行一次


async def _selftest() -> None:
    """真实 API 自检（协议类必须真实执行）：Search 取 100 个知名仓库 → nodes 批量拉取 → 校验星数 → 落盘留痕。"""
    settings = get_settings()
    async with GitHubClient(settings.github_token) as client:
        search = await client.search_repositories("stars:>50000", per_page=100)
        items = search.get("items", [])
        if len(items) < MAX_NODES_PER_QUERY:
            raise GitHubError(f"Search 仅返回 {len(items)} 个仓库，不足 {MAX_NODES_PER_QUERY} 个")
        ids = [item["node_id"] for item in items[:MAX_NODES_PER_QUERY]]
        nodes = await client.fetch_repos_by_ids(ids)

        def _has_stars(node: object) -> bool:
            return isinstance(node, dict) and isinstance(node.get("stargazerCount"), int)

        bad = [i for i, n in enumerate(nodes) if not _has_stars(n)]
        if len(nodes) != len(ids) or bad:
            raise GitHubError(f"nodes 批量校验失败：返回 {len(nodes)} 条，缺 stargazerCount 的下标 {bad}")

        executed_at = utc_now_iso()
        record = {
            "executed_at": executed_at,
            "search_query": "stars:>50000",
            "repo_count": len(nodes),
            "repos": nodes,
        }
        out_path = BASE_DIR / "data" / "t003_selftest.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        print(f"自检通过：{len(nodes)} 个仓库 stargazerCount 全部获取（执行时间 {executed_at}）")
        for node in nodes[:5]:
            print(f"  {node['nameWithOwner']}  ★{node['stargazerCount']}")
        print(f"留痕文件：{out_path}")


if __name__ == "__main__":
    asyncio.run(_selftest())
