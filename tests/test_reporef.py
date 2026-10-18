"""GitHub 仓库地址归一（direct-follow §21.2）测试：纯函数、无 IO、全程离线，穷举接受／拒绝矩阵。

对齐 spec「仓库地址解析与归一」的两个接受场景与三个拒绝场景，逐形态断言归一结果；
`parse_github_ref`（只认地址）与 `parse_repo_ref`（地址 + 裸名）两入口的分工单独断言；
`is_valid_full_name` 的边界（类型／字符集／长度 140）单独断言——它是三条归一分支共用的判定。
"""

import pytest

from app.reporef import MAX_RAW_LEN, is_valid_full_name, parse_github_ref, parse_repo_ref

# ---------- is_valid_full_name：形态校验边界 ----------


@pytest.mark.parametrize(
    "raw",
    [
        "psf/requests",
        "a/b",
        "A.B-c_d/e.f-g_h",
        "x" * 39 + "/" + "y" * 100,  # 恰好 140：owner 39＋"/"＋repo 100，边界入选
    ],
)
def test_is_valid_full_name_accepts(raw):
    """合法形态：字母数字与 -_.、所有者与仓库各一段、长度 ≤140。"""
    assert is_valid_full_name(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        None,
        123,
        ["psf/requests"],
        "",
        "psf",  # 只有 owner
        "psf/",  # repo 段为空
        "/requests",  # owner 段为空
        "psf/requests/extra",  # 三段
        "psf requests",  # 空白分隔
        "psf/req uests",  # 段内空白
        "psf/requests\n",  # 换行
        "psf/请求",  # 非 ASCII
        "psf/req*ests",  # 非法字符
        "x" * 39 + "/" + "y" * 101,  # 141：超上限一位即拒
    ],
)
def test_is_valid_full_name_rejects(raw):
    """非法形态：非 str、段数不对、空白/换行、非法字符、超 140 一律 False。"""
    assert is_valid_full_name(raw) is False


# ---------- parse_github_ref / parse_repo_ref：接受矩阵 ----------

_ACCEPTED = [
    # (输入, 期望归一结果)
    ("https://github.com/psf/requests", "psf/requests"),
    ("http://github.com/psf/requests/", "psf/requests"),  # http 与末尾斜杠
    ("https://www.github.com/psf/requests.git/", "psf/requests"),  # www + .git + 末尾斜杠
    ("https://github.com/psf/requests/tree/main?tab=readme#intro", "psf/requests"),  # 子路径/查询串/片段
    ("https://github.com/psf/requests/blob/master/README.md", "psf/requests"),  # blob 子路径
    ("https://github.com/psf/requests?tab=readme", "psf/requests"),  # 直接跟查询串
    ("https://github.com/psf/requests#intro", "psf/requests"),  # 直接跟片段
    ("https://GitHub.com/psf/requests", "psf/requests"),  # host 大小写不敏感
    ("  https://github.com/psf/requests  ", "psf/requests"),  # 首尾空白先 strip
    ("github.com/psf/requests", "psf/requests"),  # 省略协议
    ("www.github.com/psf/requests.git", "psf/requests"),
    ("git@github.com:psf/requests.git", "psf/requests"),  # SCP-like
    ("git@github.com:psf/requests", "psf/requests"),
    ("ssh://git@github.com/psf/requests.git", "psf/requests"),  # ssh:// 形态
    ("ssh://git@github.com/psf/requests.git/", "psf/requests"),
    ("https://github.com/PSF/Requests", "PSF/Requests"),  # 归一保留输入大小写（规范名由库内/GitHub 决定）
    ("git@github.com:PSF/Requests.git", "PSF/Requests"),
]


@pytest.mark.parametrize(("raw", "expected"), _ACCEPTED)
def test_parse_github_ref_accepts(raw, expected):
    """接受形态：http/https/www、子路径、查询串、片段、末尾斜杠、.git、省略协议、SCP-like、ssh://。"""
    assert parse_github_ref(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), _ACCEPTED)
def test_parse_repo_ref_accepts_address_forms(raw, expected):
    """地址形态在关注页入口（parse_repo_ref）同口径通过。"""
    assert parse_repo_ref(raw) == expected


def test_parse_repo_ref_accepts_bare_name_only_there():
    """裸 `owner/repo`：关注页入口接受且保留输入大小写，只认地址的 parse_github_ref 拒绝——两入口分工明确。"""
    assert parse_repo_ref("psf/requests") == "psf/requests"
    assert parse_repo_ref("PSF/Requests") == "PSF/Requests"
    assert parse_github_ref("psf/requests") is None
    # 裸名的粘贴近似形态按同一口径归一（末尾斜杠、.git 后缀），不原样拿去请求 GitHub
    assert parse_repo_ref("psf/requests/") == "psf/requests"
    assert parse_repo_ref("psf/requests.git") == "psf/requests"
    assert parse_github_ref("psf/requests.git") is None


# ---------- 拒绝矩阵 ----------

_REJECTED = [
    "https://gitlab.com/psf/requests",  # 非 github.com 域名
    "https://github.company.internal/psf/requests",  # 企业版
    "https://notgithub.com/psf/requests",  # 近似域
    "https://gist.github.com/psf/abcd",  # 子域（gist 不是仓库路径）
    "https://github.com.evil.com/psf/requests",  # 后缀伪装域
    "gitlab.com/psf/requests",  # 省略协议的近似域（三段，按裸名拒绝）
    "gitlab.com/psf",  # 省略协议的近似域恰两段：owner 段带点 → 不当裸名（评审 F3-2）
    "example.com/foo",  # 任意域名两段同上
    "gist.github.com/foo",  # 子域两段同上
    "psf",  # 只有 owner
    "psf/",  # 缺 repo
    "https://github.com/psf",  # 地址形态缺 repo（不得掉进裸名分支）
    "github.com/psf",  # 省略协议缺 repo（同上）
    "git@github.com:psf",  # SCP-like 缺 repo
    "https://github.com/settings/profile",  # 保留页前缀
    "https://github.com/orgs/psf",
    "https://github.com/users/psf",
    "https://github.com/topics/python",
    "https://github.com/features/actions",
    "https://github.com/marketplace/actions",
    "https://github.com/collections/awesome",  # 其余两段站内入口（评审 F3-2 补入）
    "https://github.com/security/advisories",
    "https://github.com/enterprise/foo",
    "https://github.com/account/foo",
    "https://github.com/organizations/foo",
    "https://github.com/stars/psf",  # 站内"我的星标"页
    "https://github.com/new/import",  # 新建导入页
    "https://github.com/discussions/foo",  # 讨论页入口
    "https://github.com/password_reset/foo",  # 找回密码页
    "collections/awesome",  # 裸名形态下的保留页前缀同样拒
    "https://github.com/login",
    "https://github.com/explore",
    "https://github.com/trending",
    "https://github.com/notifications",
    "https://github.com/sponsors/psf",
    "https://github.com/codespaces",
    "https://github.com/apps/dependabot",
    "settings/profile",  # 裸名形态下的保留页前缀同样拒
    "psf/requests/extra",  # 裸形态三段
    "git@github.com:psf/requests/extra",  # SCP-like 三段（非子路径形态）
    "psf requests",  # 空白分隔（两段但含空白）
    "psf/requests extra",
    "psf/req*ests",  # 非法字符
    "psf/请求",
    "./requests",  # 路径段为 "."
    "psf/..",  # 路径段为 ".."
    "..",
    ".",
    "",  # 空串
    "   ",  # 纯空白
    "\n",  # 只有换行
    "https://github.com/",  # 只有 host
    "https://github.com",  # 连路径都没有
    "github.com",  # 单写域名不算地址
    "github.com/",  # 省略协议根前缀后无内容
    "ftp://github.com/psf/requests",  # 非 http/https/ssh 协议
    "file:///psf/requests",
    "javascript:alert(1)",
    "x" * (MAX_RAW_LEN + 1),  # 超原始长度上限
    "https://github.com/psf/requests/" + "a" * 2100,  # 地址合理但原始串超长
    "x" * 39 + "/" + "y" * 101,  # 归一结果超 140
]


@pytest.mark.parametrize("raw", _REJECTED)
def test_parse_github_ref_rejects(raw):
    """拒绝形态（只认地址的入口）：一律 None——调用方据此 400，且不发起任何 GitHub 请求。"""
    assert parse_github_ref(raw) is None


@pytest.mark.parametrize("raw", _REJECTED)
def test_parse_repo_ref_rejects(raw):
    """拒绝形态（关注页入口）：一律 None；带域名/协议特征者不得被当成裸 owner/repo。"""
    assert parse_repo_ref(raw) is None


@pytest.mark.parametrize("raw", [None, 123, ["psf/requests"], {"repo": "psf/requests"}])
def test_parse_repo_ref_rejects_non_string(raw):
    """非 str 输入（表单异常/直接调用）：两个入口都返回 None，不抛异常。"""
    assert parse_repo_ref(raw) is None
    assert parse_github_ref(raw) is None


def test_max_raw_len_boundary():
    """原始长度上限的边界：恰好在限内的畸形串走形态判定（仍 None），超一位直接拒。"""
    assert parse_repo_ref("a" * MAX_RAW_LEN) is None  # 单段超长，形态不合格
    assert parse_repo_ref("a" * (MAX_RAW_LEN + 1)) is None
