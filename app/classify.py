"""分类器纯函数：语言成榜 + 主题词表匹配（《架构决策记录》§3 决策 3：分类全部本地计算）。

- 语言：6 个指定语言各自成榜（共识文档 §6），其余（含无语言）进"其它语言"；
  匹配用 GitHub primaryLanguage.name 的精确值（首字母大写，如 "TypeScript"），不做模糊匹配；
- 主题：封闭词表（config/topics.yaml）精确匹配 repositoryTopics 值（小写 kebab-case）；
  命中多主题 → 多榜重复；零命中 → 空列表，由调用方解释为"其他"榜，本模块不发明 "other" 主题 key；
- 词表由调用方显式 load_topics() 后传入，模块顶层不读文件：保持纯函数可测，路径决策留在调用方。
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import yaml

# 6 个指定语言：键是 GitHub primaryLanguage.name 的精确匹配名（API 固定首字母大写，故可直接作键），
# 值是榜单 key（小写）；dict 顺序即榜单展示顺序，与共识文档 §6 列举顺序（Java/Go/Rust/TypeScript/JavaScript/Python）一致。
LANGUAGES: dict[str, str] = {
    "Java": "java",
    "Go": "go",
    "Rust": "rust",
    "TypeScript": "typescript",
    "JavaScript": "javascript",
    "Python": "python",
}

OTHER_LANGUAGE_KEY = "other"  # 其它语言榜：未列出的语言与无语言仓库都归这里


class TopicSpec(TypedDict):
    """主题词表单项：label 为榜单展示名，words 为命中词条（小写 kebab-case）。"""

    label: str
    words: list[str]


def load_topics(path: Path) -> dict[str, TopicSpec]:
    """加载主题词表，返回 {主题key: TopicSpec}，保持 YAML 中的主题顺序（dict 保序）。

    词表是封板物（本人过目后才生效），这里只做装载不做改写；
    utf-8-sig 与 app/config.py 同口径：兼容编辑器存出的 UTF-8 BOM，避免首键名被 \ufeff 污染。
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        # 词表缺失是配置错误：报错指向文件，不让调用方在远处炸
        raise FileNotFoundError(f"主题词表不存在：{path}") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"主题词表 {path} 不是合法 YAML：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"主题词表 {path} 顶层必须是「主题key: label/words」的映射")
    return raw


def classify_language(language: str | None) -> str:
    """语言分类：精确匹配 6 个指定语言名返回其小写 key，其余与 None 返回 OTHER_LANGUAGE_KEY。

    不做 lower 归一：GitHub 固定返回首字母大写名，出现小写变体说明数据源异常，
    宁可落"其它语言"也不误进指定榜。
    """
    return LANGUAGES.get(language, OTHER_LANGUAGE_KEY)


def classify_topics(topics: list[str], table: dict[str, TopicSpec]) -> list[str]:
    """主题分类：返回命中主题的 key 列表（按词表顺序），零命中返回空列表。

    空列表即"其他"榜，由调用方解释——"其他"不是主题 key，避免与词表主题混同；
    输入防御性 lower/strip（GitHub 规范为小写 kebab-case，脏数据也不影响命中判定）。
    """
    normalized = {t.strip().lower() for t in topics}
    return [key for key, spec in table.items() if any(word in normalized for word in spec["words"])]
