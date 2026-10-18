"""GitHub 仓库地址归一（direct-follow §21.2）：把粘贴进来的地址／裸名归一为 `owner/repo`。

唯一职责是"形态归一"，纯函数、无 IO、只用标准库：解析失败一律返回 None，由调用方
（`app.web.routes` 的 `POST /follows/add`）决定用户可见文案与 HTTP 状态码——本模块不认识
HTTP，也不抛业务异常，便于单测穷举接受／拒绝矩阵。不调用 GitHub：能不能归一是本地判定，
仓库存不存在由后续单仓请求回答。

接受形态（其余一律拒绝）：
- `http`/`https` 的 `github.com` 或 `www.github.com` 链接：可带子路径（`/tree/main`、`/blob/x/y`）、
  查询串、片段、末尾斜杠、`repo` 段 `.git` 后缀；host 大小写不敏感；
- 省略协议：`github.com/owner/repo`、`www.github.com/owner/repo`；
- `git@github.com:owner/repo.git`（SCP-like，GitHub 页面「Clone with SSH」给出的形态）；
- `ssh://git@github.com/owner/repo.git`。

拒绝形态：非 github.com / www.github.com 主机（含 `gist.github.com` 这类子域、企业版与
`notgithub.com` 这类近似域）、GitHub 保留页前缀（`settings`/`orgs`/`users`/`topics`/…，见
`_RESERVED_OWNERS`）、`.` 与 `..` 路径段、非法字符、超长（沿用 owner 39＋repo 100 的既有上限
140）、空串与纯空白、输入超过 `MAX_RAW_LEN` 字符。

两个入口的分工（§21.2「裸 owner/repo 只在关注页接受」）：
- `parse_github_ref(raw)`：只认带域名／协议的形态，裸 `psf/requests` 返回 None。将来若有其它
  调用方（如搜索框）需要"只认地址"的语义，用这个；
- `parse_repo_ref(raw)`：先走 `parse_github_ref`，失败时才接受裸 `owner/repo`（关注页用）。

为什么不用第三方 URL 归一库（如 `url-normalize`、`w3lib`）：这里要的不是通用 URL 规范化
（大小写、默认端口、百分号编码、路径合并那些边角都与本场景无关），而是单站点（github.com）
的"几种已知粘贴近似形态 → owner/repo"的收敛。标准库 `urllib.parse` 只用来切出 host 与 path
两段，其余是字符集与长度校验，用到的边角案例本模块单测逐条钉死；引进通用库既不减少这里的
边角数量，还要为它不做的 git/SSH 形态另写一层。按项目「高边角密度才引库」口径，不新增依赖。
"""

from __future__ import annotations

import re

__all__ = ["MAX_RAW_LEN", "parse_github_ref", "parse_repo_ref", "is_valid_full_name"]

# 归一结果形态（沿用关注 API 既有口径：owner/repo 字符集 + owner 39＋"/"＋repo 100）
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_FULL_NAME_MAX_LEN = 140

# 原始输入长度上限：正常 GitHub 地址远短于此，超长按无法识别处理（防御为主，不作原始串落库/落日志）
MAX_RAW_LEN = 2048

# 认的 host 只有这两个（Host 名大小写不敏感；子域、企业版、近似域一律拒绝）
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})

# 省略协议的根前缀（小写比较；后随 "/" 才是地址形态，单写 "github.com" 不算）
_HOST_PREFIXES = ("github.com/", "www.github.com/")

# GitHub 保留页前缀：这些是第一段路径但**不是**仓库 owner，撞上按无法识别处理。
# 扩清单只改这一处，不散落到路由或模板（§21.2）。与仓库路径同形的站内入口也在列。
_RESERVED_OWNERS = frozenset(
    {
        "settings",
        "orgs",
        "users",
        "topics",
        "features",
        "marketplace",
        "login",
        "explore",
        "trending",
        "notifications",
        "sponsors",
        "codespaces",
        "apps",
        "collections",
        "security",
        "enterprise",
        "account",
        "organizations",
        "events",
        "readme",
        "about",
        "contact",
        "site",
        "billing",
        "sessions",
        "signup",
        "teams",
        "pricing",
        "dashboard",
        "pulls",
        "issues",
        "search",
        "stars",
        "new",
        "discussions",
        "password_reset",
    }
)


def is_valid_full_name(raw: object) -> bool:
    """`owner/repo` 形态校验（服务端唯一判定）：str、`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`、长度 ≤ 140。

    与关注 API（`POST /api/follows` 的 full_name 入参）同口径；不判保留页前缀、不判大小写——
    那些是地址归一（`parse_repo_ref`）的职责，这里只回答"能不能当 owner/repo 用"。
    """
    return isinstance(raw, str) and bool(_FULL_NAME_RE.fullmatch(raw)) and len(raw) <= _FULL_NAME_MAX_LEN


def _is_github_host(host: str) -> bool:
    """host 是否精确为 github.com / www.github.com（大小写不敏感；空 host 直接否）。"""
    return host.lower() in _GITHUB_HOSTS


def _clean_segments(path: str) -> list[str]:
    """path 按 "/" 切成路径段（§21.2）：丢掉空段（末尾斜杠、连续斜杠）、丢掉 `.git` 碎片。

    丢掉 `.git` 碎片是为了让 `.../requests.git/`（`.git` 后缀＋末尾斜杠）与 `.../requests.git`
    归一结果一致；放在切段阶段而不是"取到第二段再剥后缀"阶段，是为了不把 `.git` 当成 owner
    或 repo（`/psf/.git` 应视为只有一段、按拒绝处理）。
    """
    return [seg for seg in path.split("/") if seg and seg != ".git"]


def _to_full_name(host: str, path: str, *, subpath_ok: bool) -> str | None:
    """已知 host 与 path：校验 host 与路径段，返回 `owner/repo`；任一环节不合格返回 None。

    subpath_ok=True（带域名／协议的链接）时只取前两段，第三段起是 `/tree/...`、`/blob/...` 这类
    子路径，直接忽略；subpath_ok=False（裸名或 SCP-like）时要求恰好两段，多段按拒绝处理（§21.2）。
    段数不足、repo 段去掉 `.git` 后为空、名字非法或超长、owner 命中保留页前缀，一律 None。
    """
    if not _is_github_host(host):
        return None
    segments = _clean_segments(path)
    if len(segments) < 2 or (not subpath_ok and len(segments) > 2):
        return None
    owner = segments[0]
    repo = segments[1][:-4] if segments[1].endswith(".git") else segments[1]
    full_name = f"{owner}/{repo}"
    if not is_valid_full_name(full_name):
        return None
    # GitHub 用户名/组织名不含点号（只允许字母数字与连字符）：owner 段带点说明用户粘的是域名
    # 或站内页（如 `gitlab.com/psf`、`example.com/foo` 这类省略协议的近似域），按无法识别处理，
    # 不去 GitHub 换一个误导性的 404。
    if "." in owner:
        return None
    if owner in (".", "..") or repo in (".", "..") or owner.lower() in _RESERVED_OWNERS:
        return None
    return full_name


def parse_github_ref(raw: object) -> str | None:
    """只认带域名／协议的 GitHub 地址；归一为 `owner/repo`，失败返回 None。

    覆盖 http/https 链接（可带子路径／查询串／片段／末尾斜杠／`.git` 后缀）、省略协议的
    `github.com/owner/repo`、`git@github.com:owner/repo.git` 与 `ssh://git@github.com/owner/repo.git`。
    裸 `owner/repo`（不写域名）不在本函数职责内，返回 None——关注页的裸名兜底见 `parse_repo_ref`。
    归一结果保留输入大小写（规范名由库内记录或 GitHub 响应决定，本模块不改写大小写）。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > MAX_RAW_LEN:
        return None

    lowered = text.lower()
    if lowered.startswith("git@"):
        # SCP-like：git@host:path（GitHub「Clone with SSH」形态，分隔符是 ":" 而不是 "://"）
        host_part, sep, path = text.partition(":")
        if not sep:
            return None
        # 只认 git@<host>（userinfo 段唯一）；其余 user 前缀按非法处理
        return _to_full_name(host_part[len("git@") :], path, subpath_ok=False)

    if "://" in text:
        scheme_part, _, rest = text.partition("://")
        if scheme_part.lower() not in ("http", "https", "ssh"):
            return None
        # 拿 host、path 尾部：用 split 而非 urlsplit，host 一眼可见（本模块只关心这两段）
        authority, _, path = rest.partition("/")
        host = authority.rpartition("@")[2]  # 容忍 userinfo（ssh://git@…），host 仍是最后一段
        if not host:
            return None
        # 带域名/协议的链接（含 SSH 的 ssh://git@host/owner/repo）允许子路径、查询串、片段
        return _to_full_name(host, path.partition("#")[0].partition("?")[0], subpath_ok=True)

    # 省略协议：仅 github.com/ 与 www.github.com/ 两个根前缀，单写域名不算地址
    for prefix in _HOST_PREFIXES:
        if lowered.startswith(prefix):
            return _to_full_name(prefix[:-1], text[len(prefix) :], subpath_ok=True)
    return None


def parse_repo_ref(raw: object) -> str | None:
    """关注页入口用：先按 `parse_github_ref` 认地址形态，失败时才接受裸 `owner/repo`。

    守卫（§21.2 与 design 决策 1）：输入带域名／协议特征（含 `://`、以 `git@` 开头，或以
    `github.com/`、`www.github.com/` 开头，大小写不敏感）却没能归一成功时，直接判失败——不让
    `github.com/owner`（缺 repo）掉进裸名分支被当成 owner=`github.com` 的仓库去打 GitHub，
    那会得到误导性的 404 而不是"地址无法识别"的 400。裸名按同一归一口径处理：恰好两段，
    允许末尾斜杠与 `.git` 后缀（`psf/requests/`、`psf/requests.git` 均归一为 `psf/requests`），
    大小写与字符集仍按 `is_valid_full_name`，owner 保留页前缀同样拒绝。
    """
    parsed = parse_github_ref(raw)
    if parsed is not None:
        return parsed
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > MAX_RAW_LEN:
        return None
    lowered = text.lower()
    if "://" in text or lowered.startswith("git@") or lowered.startswith(_HOST_PREFIXES):
        return None
    segments = _clean_segments(text)
    if len(segments) != 2:
        return None
    # 裸名同样走 _to_full_name 的归一口径（丢空段、剥 `.git`、字符集与长度、保留页前缀）：
    # `psf/requests/` 与 `psf/requests.git` 这类粘贴形态归一到 `psf/requests`，而不是原样拿去 404。
    return _to_full_name("github.com", text, subpath_ok=False)
