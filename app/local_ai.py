"""本地 AI 导出/回填通道（local-ai-relay）：把"AI 生成文本"从"应用直接调远端模型"改成"导出任务 → 本地工具
生成 → 回填写入"，用于生产 AI 段停用（AI_ENABLED=0）后仍要持续产出文本的场景。

口径（任务书钉死，勿自由发挥）：
- 导出判定与每日 ensure（app.ai.recommend_missing）逐条同口径，顺序一致：translate → week → quarter →
  total → summary；复用 _scope_sets（S = 三口径榜去重 ∪ 关注集）、_load_repo_info、_ReadmeState
  （AI_README_HEAD_CHARS 截断、失败退化元数据）与三份 prompt 构造纯函数（app.ai.build_*_prompt）——
  不在本模块重写第二套口径；
- source='manual' 的行不作为重生候选（缺失补缺不受影响）；
- 窗口日护栏：total/summary 的"已有行是否重生"必须拉 README 才能判定，**两段各持一份独立探测预算**
  （同为 probe_budget，互不饿死——共享计数器会让先跑的 total 段吃光预算、summary 段整轮零判定）；
  **未判定候选如实上报**：预算耗尽跳过的、以及因本批凑满 limit 根本没跑到的（段尾"只扫名不探测"尾扫补计，
  零 I/O）都计入响应 `probe_skipped`，`probe_truncated`＝本次是否发生过未判定（同一候选只计一次；
  廉价缺失任务不计入，归 remaining）；
- 导出只读：不写库、不占运行锁、无 AI client 依赖（AI_ENABLED=0 下照常可用）；
- 回填默认只补缺失（overwrite=true 才覆盖），一律写 source='manual'；期次过期整条拒绝；逐条校验失败
  不阻断同批其他条目；translate 条目另需回带 src（导出时原文指纹），与当前原文不符即拒收——
  等价自动路径"我译的原文仍是当前原文"守卫，防过期译文静默入库。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime

from app.ai import (
    _ListedItem,
    _load_repo_info,
    _quarter_label,
    _ReadmeState,
    _refresh_window_open,
    _scope_sets,
    _week_label,
    build_recommend_prompt,
    build_summary_prompt,
    build_translate_prompt,
    has_cjk,
)
from app.collector.github import GitHubClient
from app.config import get_settings

logger = logging.getLogger(__name__)

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"  # schema 硬约定：UTC 定长（与 app.report/app.ai 同口径）

KINDS = ("translate", "week", "quarter", "total", "summary")  # 导出/回填的任务类别枚举
KIND_ANY = "all"  # 导出的 kind 参数：不过滤

# 回填文本长度上限（字符）：三类文本（推荐语/概要/译文）现状实测 85~524 字符，2000 留足余量——
# 只挡"贴错整篇文档/整段对话"这类事故，不压缩正常生成空间（prompt 要求 2~3 句或 3~5 句）。
MAX_TEXT_CHARS = 2000

# 窗口日 README 探测上限（护栏：单请求不无限拉 README）。total 段与 summary 段**各持一份**（见 build_export）。
# 为什么不随 limit 线性收紧（2026-09-13 抽查发现的原实现缺陷）：探测从 total_names/all_names 列表头部
# 开始、服务端不记状态，上限过小（原 `max(limit*3, 20)`）时，头部一段若全是"未变更"仓，每次导出都重复
# 探测同一段、返回 0 条重生任务，永远推进不到后面的变更仓（窗口日死胡同）。
# 取值依据：T-037 后单个窗口日的变更仓约 30~70 / 总星集约 700（≈5~10%），200 条候选全未命中的概率
# ≈0.9²⁰⁰≈0；调用量（2026-09-13 评审实测）：单次请求实际约 200~400 次 fetch_readme ≈ **单段预算**
# （两段候选头部重叠，_ReadmeState 缓存命中，第二段几乎不额外拉）；理论上界 = 两段各吃满 → 2×预算（≤800，
# 实战不会发生）；HTTP 路径 limit ≤ 50 → 单段预算 ≤ 400，故 PROBE_BUDGET_MAX=500 只有直接调用方够得着（防御上限）。
PROBE_BUDGET_MIN = 200
PROBE_BUDGET_MAX = 500

# 本地工具未必遵守 prompt 的"只输出正文"约束：写库前做最小清洗（只去壳，不改正文）
_FILL_PREFIXES = (
    "推荐理由：",
    "推荐语：",
    "译文：",
    "翻译：",
    "AI 概要：",
    "推荐理由:",
    "推荐语:",
    "译文:",
    "翻译:",
    "AI 概要:",
)
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』", "《": "》"}

# 原文指纹：8 位十六进制（sha1 前 8 位）——translate 任务的"我译的是哪份原文"凭据，随作业单走一趟、回填时比对
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{8}$")


def text_fingerprint(text: str) -> str:
    """原文指纹：description_en 的 sha1 前 8 位十六进制。

    用途与自动路径的 `AND description_en = ?` 同语义——译文入库前证明"我翻译的原文仍是当前原文"；
    回填路径拿不到当轮读到的原文值，故让导出把指纹随任务带走、回填时对当前值重算比对（不回带即拒收）。
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


# ---------- 导出：待生成任务判定 ----------


def _mk_item(index: int, kind: str, full_name: str, dimension: str | None, period_label: str | None,
             system: str, user: str, src: str | None = None) -> dict:
    """一条导出任务（两种输出形态共用同一份结构，保证任务集合一致）。

    src 仅 translate 类携带（其余四类不带该字段，回填侧也一律忽略）。
    """
    item = {
        "task_id": f"T{index}",
        "kind": kind,
        "repo": full_name,
        "dimension": dimension,
        "period_label": period_label,
        "system": system,
        "user": user,
    }
    if src is not None:
        item["src"] = src
    return item


def _listed_item_args(item: _ListedItem) -> tuple[int | None, int | None, float | None]:
    """上榜行 → (delta, stars, pool_days)：新区行带入池语境，主榜/新项目区行进窗口增量语境（同 recommend_missing）。"""
    if item.rising is not None:
        return item.rising.pool_delta, item.rising.stars, item.rising.pool_days
    assert item.row is not None  # _ListedItem 不变量：主榜/新项目区仓 row 恒非 None（rising/row 互斥）
    return (item.row.delta if item.row.delta is not None else 0), item.row.stars, None


async def build_export(
    conn: sqlite3.Connection,
    *,
    limit: int,
    kind: str = KIND_ANY,
    github_client: GitHubClient | None,
    now: datetime,
    log: logging.Logger | None = None,
) -> dict:
    """导出待生成任务（只读）：判定顺序与每日跑批一致（translate → week → quarter → total → summary）。

    - 覆盖集 S 与三维度/概要判定全部复用 app.ai 的既有实现与既有 prompt 构造（同一份口径，不复制第二套）；
    - `source='manual'` 的行不做重生判定（不拉 README、不比 sha）；缺失补缺不受影响；
    - README 拉取复用 _ReadmeState（AI_README_HEAD_CHARS 截断、失败退化元数据、绝不抛出）；
    - translate 任务额外带 `src`＝description_en 的 sha1 前 8 位（原路径护栏 `description_en = ?` 的
      等价凭据）：本地工具原样回带、回填时比对当前原文，不一致即拒收——防过期译文静默入库；
    - 护栏：凑满 limit 即停；窗口日 total/summary 的"已有行是否重生"必须拉 README 才能判定，
      这类探测上限 = min(max(limit*8, PROBE_BUDGET_MIN), PROBE_BUDGET_MAX)（见模块常量注释：
      上限过小会因"从列表头部开始、不记状态"撞死胡同，故保底 200、封顶 500）。
      **total 段与 summary 段各持一份独立预算**：两段候选列表长度可能悬殊，共享计数器会让先跑的
      total 段把预算吃光、summary 段整轮零判定（评审 F2-1 复现），按段独立后互不饿死；
      **未判定候选如实计数**：因预算耗尽而跳过、或因本批凑满 limit 而根本没跑到的候选，统一计入
      `probe_skipped`（后者由段尾"只扫名不探测"的尾扫补计，零 I/O），`probe_truncated`＝其是否 > 0——
      否则 limit 先被 total 段凑满时 summary 段整段零判定且零信号，调用方看到 remaining=0 就收工会漏掉
      重生（评审 F2-2'；正确收工判据＝本次导出里没有任何 total/summary 重生任务，`probe_skipped` 只是
      告警值、不是进度：服务端不记状态，重复导出不会清零，也不代表剩余工作量）。
      同一候选一轮只计一次；廉价可判的缺失任务不计入（归 remaining）；
    - remaining：廉价判定（不需要 README 探测）下仍待生成、且本次未导出的条数（含达到 limit 后被截断的部分）。

    返回 {"as_of", "week_label", "quarter_label", "window_open", "remaining", "probe_truncated",
    "probe_skipped", "items"}。
    """
    log = log or logger
    listed_by_period, follow_names = _scope_sets(conn, now=now)
    all_names = list(follow_names)
    for period in ("week", "quarter", "total"):
        for full_name in listed_by_period[period]:
            if full_name not in all_names:
                all_names.append(full_name)
    repo_info = _load_repo_info(conn, all_names)
    week_label = _week_label(now.date())
    quarter_label = _quarter_label(now.date())
    window_open = _refresh_window_open(now.date())
    total_names = list(follow_names)
    for full_name in listed_by_period["total"]:
        if full_name not in total_names:
            total_names.append(full_name)

    existing: dict[tuple[int, str, str], sqlite3.Row] = {}
    for row in conn.execute(
        "SELECT repo_id, dimension, period_label, readme_sha, generated_week, source FROM recommendations"
    ):
        existing[(row["repo_id"], row["dimension"], row["period_label"])] = row

    readme_state = _ReadmeState(github_client, log, max_chars=get_settings().ai_readme_head_chars)
    stats = {"readme_fetched": 0}

    def wanted(task_kind: str) -> bool:
        return kind == KIND_ANY or kind == task_kind

    # 廉价待办（不需要 README 探测即可判定）：既决定本次导出哪些缺失任务，也用于算 remaining。
    # 窗口日"已有行是否重生"不是廉价判定（要拉 README 比 sha），故不进本集合、由下方探测段处理。
    cheap_keys: set[tuple[str, str, str | None]] = set()
    if wanted("translate"):
        for full_name in all_names:
            info = repo_info.get(full_name)
            if info is None or info["description_zh"] is not None:
                continue
            text_en = info["description_en"]
            if not text_en or not text_en.strip():
                continue  # GitHub 官方允许无简介：空描述跳过
            if has_cjk(text_en):
                continue  # 原文已含中文（含中英混排），不重复译
            cheap_keys.add(("translate", full_name, None))
    if wanted("week"):
        for full_name in listed_by_period["week"]:
            info = repo_info.get(full_name)
            if info is None or (info["id"], "week", week_label) in existing:
                continue
            cheap_keys.add(("week", full_name, week_label))
    if wanted("quarter"):
        for full_name in listed_by_period["quarter"]:
            info = repo_info.get(full_name)
            if info is None:
                continue
            cur = existing.get((info["id"], "quarter", quarter_label))
            if cur is None:
                cheap_keys.add(("quarter", full_name, quarter_label))
            elif window_open and cur["generated_week"] != week_label and cur["source"] != "manual":
                cheap_keys.add(("quarter", full_name, quarter_label))  # 窗口日且非人工行：重生（廉价判定）
    if wanted("total"):
        for full_name in total_names:
            info = repo_info.get(full_name)
            if info is None or (info["id"], "total", "all") in existing:
                continue
            cheap_keys.add(("total", full_name, "all"))
    if wanted("summary"):
        for full_name in all_names:  # 概要段覆盖 S 全集（口径同每日 ensure 的 refresh 路径）
            info = repo_info.get(full_name)
            if info is None or (info["id"], "summary", "all") in existing:
                continue
            cheap_keys.add(("summary", full_name, "all"))

    items: list[dict] = []
    # 窗口日 README 探测上限：按 limit 缩放但保底 PROBE_BUDGET_MIN、封顶 PROBE_BUDGET_MAX（取值依据见模块常量注释）。
    # 计数器按段独立（total / summary 各一份同值预算）：两段候选列表长度可能悬殊，共享计数器会让先跑的
    # total 段吃光预算、summary 段整轮零判定（评审 F2-1 复现：total 段 200 次探测后 remainder 全被跳过）。
    probe_budget = min(max(limit * 8, PROBE_BUDGET_MIN), PROBE_BUDGET_MAX)
    probed_by_kind = {"total": 0, "summary": 0}
    # 未判定候选数（两段合计）：预算耗尽跳过的 ＋ 因凑满 limit 没跑到的（尾扫补计）；进响应供调用方判断推进方式
    probe_skipped = 0

    def full() -> bool:
        return len(items) >= limit

    def count_probe_candidates(names: list[str], dimension: str) -> int:
        """尾扫（只扫名、零 I/O）：段循环因凑满 limit 提前结束时，统计尾部"本该探测而未判定"的候选数。

        只数需要 README 判定的重生态（已有行 ＋ 窗口日 ＋ 非人工行）；廉价缺失任务不算（它们由 `remaining`
        反映）。尾部候选本轮既未被探测、也未被预算跳过、也未导出，故不会重复计数（评审 F2-2'：
        没有这段尾扫时，limit 先被 total 段凑满 → summary 段整段零判定且零信号）。
        """
        count = 0
        for full_name in names:
            info = repo_info.get(full_name)
            if info is None:
                continue
            cur = existing.get((info["id"], dimension, "all"))
            if cur is None or not window_open or cur["source"] == "manual":
                continue
            count += 1
        return count

    # --- 翻译段：S 内 description_zh IS NULL、英文非空、不含 CJK；任务带原文指纹 src（回填时比对同一条原文） ---
    if wanted("translate"):
        for full_name in all_names:
            if full():
                break
            if ("translate", full_name, None) not in cheap_keys:
                continue
            info = repo_info[full_name]
            text_en = info["description_en"]
            system, user, _ = build_translate_prompt(text_en)
            items.append(
                _mk_item(
                    len(items) + 1, "translate", full_name, None, None, system, user, src=text_fingerprint(text_en)
                )
            )

    # --- 周榜段：缺 (repo_id, 'week', 当周标签) ---
    if wanted("week"):
        for full_name, listed in listed_by_period["week"].items():
            if full():
                break
            if ("week", full_name, week_label) not in cheap_keys:
                continue
            info = repo_info.get(full_name)
            if info is None:  # 防御：进 cheap_keys 的仓必有 repo_info
                continue
            readme_text, _ = await readme_state.get(full_name, stats)
            delta, stars, pool_days = _listed_item_args(listed)
            system, user, _ = build_recommend_prompt(
                full_name=full_name,
                description=info["description_zh"] or info["description_en"] or "（无简介）",
                language=info["language"] or "未知",
                categories=listed.categories,
                dimension="week",
                delta=delta,
                stars=stars,
                readme=readme_text,
                pool_days=pool_days,
            )
            items.append(_mk_item(len(items) + 1, "week", full_name, "week", week_label, system, user))

    # --- 季榜段：缺当季行，或窗口日既有行 generated_week ≠ 当周（非人工行） ---
    if wanted("quarter"):
        for full_name, listed in listed_by_period["quarter"].items():
            if full():
                break
            if ("quarter", full_name, quarter_label) not in cheap_keys:
                continue
            info = repo_info.get(full_name)
            if info is None:
                continue
            readme_text, _ = await readme_state.get(full_name, stats)
            delta, stars, pool_days = _listed_item_args(listed)
            system, user, _ = build_recommend_prompt(
                full_name=full_name,
                description=info["description_zh"] or info["description_en"] or "（无简介）",
                language=info["language"] or "未知",
                categories=listed.categories,
                dimension="quarter",
                delta=delta,
                stars=stars,
                readme=readme_text,
                pool_days=pool_days,
            )
            items.append(_mk_item(len(items) + 1, "quarter", full_name, "quarter", quarter_label, system, user))

    # --- 总星段：缺行（廉价），或窗口日既有非人工行且 README blob sha 变化（需探测） ---
    if wanted("total"):
        for index, full_name in enumerate(total_names):
            if full():
                # 尾扫：limit 被凑满时尾部候选一律未判定，如实计入 probe_skipped（零 I/O，见函数 docstring）
                probe_skipped += count_probe_candidates(total_names[index:], "total")
                break
            info = repo_info.get(full_name)
            if info is None:
                continue
            key = (info["id"], "total", "all")
            cur = existing.get(key)
            if ("total", full_name, "all") not in cheap_keys:
                if cur is None or not window_open or cur["source"] == "manual":
                    continue  # 非窗口日/人工行：已有行不重生（人工行不拉 README、不比 sha）
                if probed_by_kind["total"] >= probe_budget:
                    probe_skipped += 1  # 预算耗尽：本轮不判定该候选（留待下次导出推进）
                    continue
                probed_by_kind["total"] += 1
                readme_text, sha = await readme_state.get(full_name, stats)
                if sha is None or cur["readme_sha"] == sha:
                    continue  # F2-1 守卫：未取到 sha / 指纹未变 → 不重生
            else:
                readme_text, _ = await readme_state.get(full_name, stats)  # 缺失补缺：README 作 prompt 输入
            listed = listed_by_period["total"].get(full_name)
            system, user, _ = build_recommend_prompt(
                full_name=full_name,
                description=info["description_zh"] or info["description_en"] or "（无简介）",
                language=info["language"] or "未知",
                categories=listed.categories if listed is not None else [],
                dimension="total",
                delta=None,  # 总星维度无增量概念；prompt 不引用数字
                stars=None,
                readme=readme_text,
            )
            items.append(_mk_item(len(items) + 1, "total", full_name, "total", "all", system, user))

    # --- 概要段：口径同 total 段（缺行廉价；窗口日既有非人工行比 sha），prompt 走 build_summary_prompt ---
    if wanted("summary"):
        for index, full_name in enumerate(all_names):
            if full():
                # 尾扫：summary 段往往在这一步直接结束（total 段先凑满 limit）——没有它则整段零判定且零信号
                probe_skipped += count_probe_candidates(all_names[index:], "summary")
                break
            info = repo_info.get(full_name)
            if info is None:
                continue
            cur = existing.get((info["id"], "summary", "all"))
            if ("summary", full_name, "all") not in cheap_keys:
                if cur is None or not window_open or cur["source"] == "manual":
                    continue
                if probed_by_kind["summary"] >= probe_budget:
                    probe_skipped += 1  # summary 段独立预算：不受 total 段已用探测数影响
                    continue
                probed_by_kind["summary"] += 1
                readme_text, sha = await readme_state.get(full_name, stats)
                if sha is None or cur["readme_sha"] == sha:
                    continue
            else:
                readme_text, _ = await readme_state.get(full_name, stats)
            system, user, _ = build_summary_prompt(
                full_name=full_name,
                description=info["description_zh"] or info["description_en"] or "（无简介）",
                language=info["language"] or "未知",
                readme=readme_text,
            )
            items.append(_mk_item(len(items) + 1, "summary", full_name, "summary", "all", system, user))

    exported = {(item["kind"], item["repo"], item["period_label"]) for item in items}
    return {
        "as_of": now.strftime(_ISO_FMT),
        "week_label": week_label,
        "quarter_label": quarter_label,
        "window_open": window_open,
        "remaining": len(cheap_keys - exported),
        "probe_truncated": probe_skipped > 0,  # 有候选未判定（预算耗尽 或 本批已凑满 limit）即为 True
        "probe_skipped": probe_skipped,
        "items": items,
    }


def render_export_text(payload: dict) -> str:
    """format=text 的自包含作业单：导出时刻/期次 + 输出格式要求 + 逐条任务块（system 与 user 全文）。

    整段可粘贴给本地对话式 AI；任务集合与 format=json 完全一致（同一份 payload 渲染）；
    translate 任务块额外回显 src（原文指纹），要求本地工具原样回带。
    """
    items = payload["items"]
    lines = [
        "GitHub 热门项目雷达 · 本地 AI 生成作业单",
        f"导出时刻：{payload['as_of']}",
        f"当期期次：周 {payload['week_label']}｜季 {payload['quarter_label']}"
        f"｜半月重生窗口：{'开' if payload['window_open'] else '关'}",
        f"任务条数：{len(items)}｜未导出的剩余待办（廉价判定）：{payload['remaining']}",
    ]
    if payload["probe_truncated"]:
        lines.append(
            f"提示：另有 {payload['probe_skipped']} 个候选判定未做（同一仓库的总星/概要各算一次；"
            "探测预算耗尽或本批已凑满单批条数）。该数字是告警值、不是进度：服务端不记状态，"
            "重复导出不会清零，也不代表剩余工作量。"
            "收工判据＝本次导出里没有任何 total/summary 重生任务；若有，就再导一批——先把本批生成结果回填，"
            "再导出下一批（已回填的行是人工行，不再占用探测预算）。"
        )
    lines += [
        "",
        "【输出格式要求】",
        "逐条完成任务后，只输出一个 JSON 对象（整段复制回填，前后不要加任何解释）：",
        '{"items": [{"repo": "owner/repo", "kind": "week", "period_label": "2026-W37", "text": "生成的正文"}]}',
        "- repo / kind / period_label 原样照抄每条任务块的「回填字段」行（translate 类的 period_label 写 null）；",
        "- translate 条目必须原样回带 src（漏带或改动会被拒收）："
        '{"repo": "owner/repo", "kind": "translate", "period_label": null, "src": "1a2b3c4d", "text": "译文正文"}；'
        "其余类别不要带 src；",
        "- text 只写正文：不要加引号包裹，不要加“推荐理由：/推荐语：/译文：/翻译：/AI 概要：”等前缀，不要解释，不要用列表或标题；",
        f"- {len(items)} 条任务就输出 {len(items)} 个对象，一条都不能少（kind 取值：translate/week/quarter/total/summary）。",
        "- 生成时把该任务块的 system 与 user 两段原文一并交给本地 AI（user 段已含 README 节选，可直接使用）。",
    ]
    if not items:
        lines += ["", "本次没有待生成任务。"]
        return "\n".join(lines) + "\n"
    for index, item in enumerate(items, 1):
        label = item["period_label"] if item["period_label"] is not None else "null"
        fields = f"repo={item['repo']}｜kind={item['kind']}｜period_label={label}"
        if "src" in item:
            fields += f"｜src={item['src']}"
        lines += [
            "",
            f"【任务 {index}/{len(items)}】",
            f"回填字段：{fields}",
            "--- system ---",
            item["system"],
            "--- user ---",
            item["user"],
            "--- 任务结束 ---",
        ]
    return "\n".join(lines) + "\n"


# ---------- 回填：解析、清洗、校验与写入 ----------


def _strip_fence(text: str) -> str:
    """剥 markdown 代码围栏（本地工具常把 JSON 包在 ```json 里；容错思路同 app.ai._parse_suggested_topics）。"""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _loads_first_json(text: str) -> object | None:
    """先按原文解析；失败则取首个 { / [ 到最后一个 } / ] 之间再解析（容忍前后夹带的说明文字）。"""
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    sliced = None
    if starts:
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            sliced = text[start : end + 1]
    for candidate in (text, sliced):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    return None


def parse_fill_payload(raw: str | bytes) -> tuple[list, bool]:
    """解析回填请求体 → (items, overwrite)。

    外层形状兼容两种：{"items": [...], "overwrite": true} 与裸数组 [...]（等价 overwrite=False）；
    解析前剥 markdown 围栏并做括号截取（本地工具输出可直接整段粘贴）。
    解析不出 JSON / 形状不对 → ValueError（路由层映射 400，文案给可操作指引）。
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"请求体不是 UTF-8 文本：{exc}") from None
    payload = _loads_first_json(_strip_fence(raw.strip()))
    if payload is None:
        raise ValueError(
            '请求体不是合法 JSON：应提交 {"items": [{"repo": "...", "kind": "...", "period_label": "...",'
            ' "text": "..."}]} 或裸数组（本地工具的 JSON 输出可整段粘贴）'
        )
    if isinstance(payload, list):
        return payload, False  # 裸数组：等价 overwrite=false（只补缺失）
    if not isinstance(payload, dict):
        raise ValueError(f"请求体应为 JSON 对象或数组，收到：{type(payload).__name__}")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError(
            'JSON 对象缺少 items 数组：应提交 {"items": [{"repo": "...", "kind": "...", "period_label": "...",'
            ' "text": "..."}], "overwrite": false}'
        )
    overwrite = payload.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise ValueError(f"overwrite 应为布尔值（true/false），收到：{overwrite!r}")
    return items, overwrite


def clean_fill_text(raw: str) -> str:
    """最小清洗：去首尾空白、去成对包裹的引号（中英文）、去"推荐理由：/推荐语：/译文：/翻译：/AI 概要："类前缀。

    只去壳不改正文（正文内部一律不动）；去壳至多三轮（双层壳如「"推荐理由：…"」也能剥净），
    清洗后仍为空 → 调用方按格式错误拒绝。前缀清单与各类 prompt 的"不要加前缀"要求一一对应
    （概要 prompt 钉的是"AI 概要："，故这里也收）。
    """
    text = raw.strip()
    for _ in range(3):
        stripped = text
        if len(stripped) >= 2 and stripped[-1] == _QUOTE_PAIRS.get(stripped[0]):
            stripped = stripped[1:-1].strip()  # 成对才去：单个引号属正文
        for prefix in _FILL_PREFIXES:
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :].strip()
                break
        if stripped == text:
            break
        text = stripped
    return text


async def apply_fill(
    conn: sqlite3.Connection,
    *,
    items: list,
    overwrite: bool = False,
    github_client: GitHubClient | None,
    now: datetime,
    log: logging.Logger | None = None,
) -> dict:
    """回填写入：逐条校验 + 清洗 + 条件写入，单条即 commit，坏条不阻断同批其余条目。

    - 默认只补缺失（目标行/目标字段已具备 → skipped），overwrite=True 才覆盖；
    - 一律写 source='manual'（人工行，自动路径的半月窗口重生不再触碰）；
    - week/quarter：generated_week=当周标签、readme_sha=NULL（与自动路径同口径）；
    - total/summary：用回填时刻重新拉取的 README blob sha 作指纹；拉取失败写 NULL（人工行不参与窗口日
      比对，该指纹仅在该行日后被手动"重新生成"重写为 ai 行时才参与比 sha）；
    - translate：src（原文指纹）必填，须等于当前 description_en 的 sha1[:8]（导出后原文变更即拒收，
      与自动路径"我译的原文仍是当前原文"守卫同语义）；通过后走既有护栏
      UPDATE ... WHERE description_zh IS NULL AND description_en 未变，不满足且未开 overwrite → skipped；
    - 非 translate 条目的 src 字段一律忽略（带了也不校验）；
    - 校验失败（repo 不在库/kind 非法/期次过期/src 缺失或过期/清洗后为空/超长）进 errors，不阻断同批。

    返回 {"written", "skipped", "failed", "errors": [{"repo", "kind", "reason"}]}。
    """
    log = log or logger
    week_label = _week_label(now.date())
    quarter_label = _quarter_label(now.date())
    readme_state = _ReadmeState(github_client, log, max_chars=get_settings().ai_readme_head_chars)
    stats = {"readme_fetched": 0}
    written = skipped = 0
    errors: list[dict] = []

    def fail(repo: object, kind: object, reason: str) -> None:
        errors.append(
            {
                "repo": repo if isinstance(repo, str) else None,
                "kind": kind if isinstance(kind, str) else None,
                "reason": reason,
            }
        )

    for raw_item in items:
        if not isinstance(raw_item, dict):
            fail(None, None, f"条目应为 JSON 对象（含 repo/kind/period_label/text），收到：{type(raw_item).__name__}")
            continue
        repo, kind = raw_item.get("repo"), raw_item.get("kind")
        period_label, text = raw_item.get("period_label"), raw_item.get("text")
        if not isinstance(repo, str) or not repo.strip():
            fail(repo, kind, f"repo 缺失或不是字符串，收到：{repo!r}")
            continue
        repo = repo.strip()
        if not isinstance(kind, str) or kind not in KINDS:
            fail(repo, kind, f"kind 应为 {'/'.join(KINDS)} 之一，收到：{kind!r}")
            continue
        if not isinstance(text, str):
            fail(repo, kind, f"text 缺失或不是字符串，收到：{text!r}")
            continue
        cleaned = clean_fill_text(text)
        if not cleaned:
            fail(repo, kind, "text 清洗后为空（只收到引号/前缀等空壳）")
            continue
        if len(cleaned) > MAX_TEXT_CHARS:
            fail(repo, kind, f"text 超过 {MAX_TEXT_CHARS} 字符上限（清洗后 {len(cleaned)} 字符）")
            continue
        # 期次绑定：week/quarter 必须与当前期次一致（跨期回填的文本页面取不到，等于白干）；
        # total/summary 固定 'all'；translate 写 repos.description_zh，无期次概念（忽略该字段）
        if kind == "week" and period_label != week_label:
            fail(repo, kind, f"周期次不一致（过期）：当前 {week_label}，收到 {period_label!r}——请重新导出后再回填")
            continue
        if kind == "quarter" and period_label != quarter_label:
            fail(repo, kind, f"季期次不一致（过期）：当前 {quarter_label}，收到 {period_label!r}——请重新导出后再回填")
            continue
        if kind in ("total", "summary") and period_label != "all":
            fail(repo, kind, f"{kind} 类 period_label 固定为 all，收到：{period_label!r}")
            continue

        repo_row = conn.execute(
            "SELECT id, description_en FROM repos WHERE full_name = ?", (repo,)
        ).fetchone()
        if repo_row is None:
            fail(repo, kind, "仓库不在跟踪池（repos 表无此 full_name）")
            continue

        if kind == "translate":
            # 原文指纹校验（等价自动路径的 `AND description_en = ?`）：src 必填、须为 8 位十六进制、
            # 且等于当前原文的 sha1[:8]——不一致即"我译的不是现在这份原文"，拒收并提示重新导出该条
            src = raw_item.get("src")
            if not isinstance(src, str) or not src.strip():
                fail(repo, kind, "translate 条目缺少 src（导出时的原文指纹）：请从作业单任务块的「回填字段」行原样照抄")
                continue
            src = src.strip().lower()
            if not _FINGERPRINT_RE.fullmatch(src):
                fail(repo, kind, f"src 非法（应为 8 位十六进制原文指纹），收到：{src!r}")
                continue
            # 原文为空时按空串指纹比对：该场景导不出 translate 任务，真走到这里也会被下方护栏拦成 skipped
            current_src = text_fingerprint(repo_row["description_en"] or "")
            if src != current_src:
                fail(repo, kind, f"原文已变更（导出指纹 {src} ≠ 当前 {current_src}），请重新导出该条")
                continue
            if overwrite:
                conn.execute(
                    "UPDATE repos SET description_zh = ? WHERE id = ?", (cleaned, repo_row["id"])
                )
                written += 1
            else:
                cur = conn.execute(
                    "UPDATE repos SET description_zh = ? WHERE id = ? AND description_zh IS NULL"
                    " AND description_en = ?",
                    (cleaned, repo_row["id"], repo_row["description_en"]),
                )
                if cur.rowcount:
                    written += 1
                else:
                    # 已译（description_zh 非空）或原文为空（description_en=NULL，等式不成立）→ 不硬写：
                    # 这两态由既有采集/翻译路径负责（改简介时采集层清 description_zh 并连带清推荐语）
                    skipped += 1
            conn.commit()
            continue

        row_exists = (
            conn.execute(
                "SELECT 1 FROM recommendations WHERE repo_id = ? AND dimension = ? AND period_label = ?",
                (repo_row["id"], kind, period_label),
            ).fetchone()
            is not None
        )
        if row_exists and not overwrite:
            skipped += 1
            continue
        if kind in ("week", "quarter"):
            sha = None  # 与自动路径同口径：周/季维度不记 README 指纹
        else:
            _, sha = await readme_state.get(repo, stats)  # 回填时刻重取指纹；拉取失败/空 → NULL
        conn.execute(
            "INSERT OR REPLACE INTO recommendations (repo_id, dimension, period_label, text, readme_sha,"
            " generated_week, source) VALUES (?, ?, ?, ?, ?, ?, 'manual')",
            (repo_row["id"], kind, period_label, cleaned, sha, week_label),
        )
        conn.commit()
        written += 1

    log.info("本地回填写入：写入 %d、跳过 %d、失败 %d", written, skipped, len(errors))
    return {"written": written, "skipped": skipped, "failed": len(errors), "errors": errors}
