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


def test_language_known_six_mapped_to_lowercase_keys():
    # 6 个指定语言各自成榜（共识文档 §6）：GitHub primaryLanguage.name 精确名 → 小写榜 key
    assert classify_language("Java") == "java"
    assert classify_language("Go") == "go"
    assert classify_language("Rust") == "rust"
    assert classify_language("TypeScript") == "typescript"
    assert classify_language("JavaScript") == "javascript"
    assert classify_language("Python") == "python"


def test_language_unknown_and_none_go_to_other():
    assert classify_language("HTML") == OTHER_LANGUAGE_KEY
    assert classify_language("C++") == OTHER_LANGUAGE_KEY
    assert classify_language(None) == OTHER_LANGUAGE_KEY


def test_language_exact_match_not_fuzzy():
    # 精确匹配契约：大小写变体不归一——数据源异常时宁可落"其它语言"也不误进指定榜
    assert classify_language("typescript") == OTHER_LANGUAGE_KEY
    assert classify_language("JAVA") == OTHER_LANGUAGE_KEY


def test_language_display_order_matches_consensus_doc():
    # 榜单展示顺序 = 共识文档 §6 列举顺序（Java/Go/Rust/TypeScript/JavaScript/Python）
    assert list(LANGUAGES.items()) == [
        ("Java", "java"),
        ("Go", "go"),
        ("Rust", "rust"),
        ("TypeScript", "typescript"),
        ("JavaScript", "javascript"),
        ("Python", "python"),
    ]


# ---------- 主题分类 ----------


def test_classify_topics_single_hit():
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["react"], table) == ["frontend"]


def test_classify_topics_multi_theme_hits_duplicate_boards():
    # 命中多主题 → 多榜重复（架构决策记录 §3 决策 3）
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["react", "docker"], table) == ["frontend", "backend"]


def test_classify_topics_same_theme_words_count_once():
    # 同一主题命中多个词条：只进一次榜，不重复计数
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["react", "vue", "tailwindcss"], table) == ["frontend"]


def test_classify_topics_result_follows_table_order_not_input_order():
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["docker", "react"], table) == ["frontend", "backend"]


def test_classify_topics_zero_hit_returns_empty_list():
    # 零命中返回空列表：调用方解释为"其他"榜，分类器不发明 "other" 主题 key
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["not-a-known-topic"], table) == []
    assert classify_topics([], table) == []


def test_classify_topics_defensive_lower_strip():
    # 输入防御性归一：GitHub 规范是小写 kebab-case，脏数据（大写/空白）不影响命中判定
    table = load_topics(TOPICS_PATH)
    assert classify_topics(["React", " Docker ", "DOCKER"], table) == ["frontend", "backend"]


# ---------- 词表装载与封板一致性 ----------


def test_topics_table_nine_themes_in_yaml_order():
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
    }


def test_topics_table_word_counts_locked_to_architecture_doc():
    # 词条数量锁死：任一主题增删词条都会漂移，测试即告警——封板物不许偷偷改
    table = load_topics(TOPICS_PATH)
    assert {key: len(table[key]["words"]) for key in table} == {
        "ai": 21,
        "frontend": 18,
        "backend": 17,
        "data": 17,
        "devops": 13,
        "devtools": 14,
        "security": 12,
        "game": 12,
        "automation": 12,
    }


def test_load_topics_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="主题词表不存在"):
        load_topics(tmp_path / "不存在.yaml")
