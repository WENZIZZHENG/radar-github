"""分类器单测：语言分类 + 主题词表匹配，纯函数离线可跑，不碰网络与数据库。

词表一致性断言直接读仓库内 config/topics.yaml，锁死与《架构决策记录》附录（封板物）的同步。
"""

from pathlib import Path

import pytest

from app.classify import (
    LANGUAGES,
    OTHER_LANGUAGE_KEY,
    classify_language,
    classify_topics,
    load_topics,
)

TOPICS_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"


# ---------- 语言分类 ----------


def test_language_known_eleven_mapped_to_lowercase_keys():
    # 11 个指定语言各自成榜（共识文档 §6 列举顺序＋新增五语言）：GitHub primaryLanguage.name 精确名 → 小写榜 key
    assert classify_language("Java") == "java"
    assert classify_language("Go") == "go"
    assert classify_language("Rust") == "rust"
    assert classify_language("TypeScript") == "typescript"
    assert classify_language("JavaScript") == "javascript"
    assert classify_language("Python") == "python"
    # 新增五语言（行为变更点：C++ 等从"其它语言"移入各自语言榜）
    assert classify_language("C++") == "cpp"
    assert classify_language("C") == "c"
    assert classify_language("C#") == "csharp"
    assert classify_language("Kotlin") == "kotlin"
    assert classify_language("Swift") == "swift"


def test_language_unknown_and_none_go_to_other():
    assert classify_language("HTML") == OTHER_LANGUAGE_KEY
    assert classify_language("PHP") == OTHER_LANGUAGE_KEY
    assert classify_language(None) == OTHER_LANGUAGE_KEY


def test_language_exact_match_not_fuzzy():
    # 精确匹配契约：大小写变体不归一——数据源异常时宁可落"其它语言"也不误进指定榜
    assert classify_language("typescript") == OTHER_LANGUAGE_KEY
    assert classify_language("JAVA") == OTHER_LANGUAGE_KEY


def test_language_display_order_matches_consensus_doc():
    # 榜单展示顺序 = 共识文档 §6 列举顺序（既有 6 项序不变）＋新增五语言插尾部（C++/C/C#/Kotlin/Swift）
    assert list(LANGUAGES.items()) == [
        ("Java", "java"),
        ("Go", "go"),
        ("Rust", "rust"),
        ("TypeScript", "typescript"),
        ("JavaScript", "javascript"),
        ("Python", "python"),
        ("C++", "cpp"),
        ("C", "c"),
        ("C#", "csharp"),
        ("Kotlin", "kotlin"),
        ("Swift", "swift"),
    ]


# ---------- 主题分类 ----------


def test_classify_topics_single_hit():
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["react"], table) == ["frontend"]


def test_classify_topics_multi_theme_hits_duplicate_boards():
    # 命中多主题 → 多榜重复（架构决策记录 §3 决策 3）
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["react", "kubernetes"], table) == ["frontend", "backend"]


def test_classify_topics_same_theme_words_count_once():
    # 同一主题命中多个词条：只进一次榜，不重复计数
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["react", "vue", "tailwindcss"], table) == ["frontend"]


def test_classify_topics_result_follows_table_order_not_input_order():
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["kubernetes", "react"], table) == ["frontend", "backend"]


def test_classify_topics_singular_rule_hits_plural_topic():
    # 决策 3 v2 单复数归一：词条精确命中、或 topics 值 = 词条＋尾"s" 亦命中（词表只写单数即可覆盖复数 topics）
    # 迷你表直证 +s 规则本身（不依赖词表显式补词）：
    table = {"ai": {"label": "AI", "words": ["ai-agent"]}}
    assert classify_topics(["ai-agents"], table) == ["ai"]
    # 真实词表：deepseek-harness 场景（topics 为复数 ai-agents）应进 ai 主题
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["ai-agents"], table) == ["ai"]


def test_classify_topics_singular_rule_is_one_way():
    # 决策 3 v2 单向：不做反向（词条复数不命中单数 topic）、不做通用去 s——防 css 被剥成 cs 之类误伤
    table = {"ai": {"label": "AI", "words": ["ai-agents"]}}
    assert classify_topics(["ai-agent"], table) == []
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["cs"], table) == []


def test_classify_topics_exact_hit_unchanged():
    # 决策 3 v2 只在精确命中之外加了 +s 变体：既有精确命中行为不变
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["ai-agent"], table) == ["ai"]
    assert classify_topics(["react"], table) == ["frontend"]


def test_classify_topics_zero_hit_returns_empty_list():
    # 零命中返回空列表：调用方解释为"其他"榜，分类器不发明 "other" 主题 key
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["not-a-known-topic"], table) == []
    assert classify_topics([], table) == []


def test_classify_topics_defensive_lower_strip():
    # 输入防御性归一：GitHub 规范是小写 kebab-case，脏数据（大写/空白）不影响命中判定
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["React", " Kubernetes ", "KUBERNETES"], table) == ["frontend", "backend"]


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("leetcode", "learning"),
        ("android", "mobile"),
        ("esp32", "embedded"),
        ("jellyfin", "media"),
        ("home-assistant", "selfhosted"),
        ("solidity", "blockchain"),
    ],
)
def test_classify_topics_new_theme_exact_hit(topic, expected):
    # 扩容 6 主题各一条精确命中：命中行为仍走既有精确规则，零匹配逻辑改动
    table = load_topics(TOPICS_PATH)
    assert classify_topics([topic], table) == [expected]


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("cheatsheets", "learning"),
        ("androids", "mobile"),
        ("fpgas", "embedded"),
        ("podcasts", "media"),
        ("nextclouds", "selfhosted"),
        ("wallets", "blockchain"),
    ],
)
def test_classify_topics_new_theme_singular_rule(topic, expected):
    # 扩容 6 主题同样吃决策 3 v2 尾"s"归一：词表只收单数，复数 topics 自动命中
    table = load_topics(TOPICS_PATH)
    assert classify_topics([topic], table) == [expected]


def test_classify_topics_algorithms_singular_rule_hits_learning():
    # 词形修正回归锁（2026-10-11 裁定）：learning 只收单数 algorithm——topics 复数 algorithms 由尾"s"归一命中
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["algorithms"], table) == ["learning"]


def test_classify_topics_y_ies_plural_variant_hits_blockchain():
    # y→ies 变体不受尾"s"归一覆盖（词条 cryptocurrency 只命中 cryptocurrency/cryptocurrencys）→ 复数显式收录
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["cryptocurrencies"], table) == ["blockchain"]


def test_classify_topics_dual_board_flutter_hits_frontend_and_mobile():
    # 双榜破例首例（2026-10-11 拍板）：mobile 与 frontend 共有 5 词、frontend 不搬家 → 标该词的仓同时进两榜
    # 结果顺序 = 词表顺序（frontend 在前）
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["flutter"], table) == ["frontend", "mobile"]


# ---------- 词表装载与封板一致性 ----------


def test_topics_table_fifteen_themes_in_yaml_order():
    table = load_topics(TOPICS_PATH)
    assert list(table.keys()) == [
        "ai",
        "frontend",
        "backend",
        "data",
        "devops",
        "devtools",
        "security",
        "game",
        "automation",
        "learning",
        "mobile",
        "embedded",
        "media",
        "selfhosted",
        "blockchain",
    ]


def test_topics_table_labels_match_architecture_doc():
    table = load_topics(TOPICS_PATH)
    assert {key: table[key]["label"] for key in table} == {
        "ai": "AI与智能",
        "frontend": "前端/UI",
        "backend": "后端/云原生",
        "data": "数据库与数据工程",
        "devops": "DevOps/监控",
        "devtools": "开发者工具",
        "security": "安全",
        "game": "游戏/图形",
        "automation": "自动化/脚本",
        "learning": "学习资源",
        "mobile": "移动",
        "embedded": "嵌入式/硬件",
        "media": "音视频/媒体",
        "selfhosted": "自托管/家居",
        "blockchain": "区块链/金融",
    }


def test_topics_table_word_counts_locked_to_architecture_doc():
    # 词条数量锁死：任一主题增删词条都会漂移，测试即告警——封板物不许偷偷改
    table = load_topics(TOPICS_PATH)
    assert {key: len(table[key]["words"]) for key in table} == {
        "ai": 122,
        "frontend": 46,
        "backend": 54,
        "data": 36,
        "devops": 21,
        "devtools": 35,
        "security": 41,
        "game": 19,
        "automation": 18,
        "learning": 16,
        "mobile": 8,
        "embedded": 12,
        "media": 14,
        "selfhosted": 6,
        "blockchain": 14,
    }


def test_load_topics_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="主题词表不存在"):
        load_topics(tmp_path / "不存在.yaml")
