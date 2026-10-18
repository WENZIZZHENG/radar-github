"""规范文本机械复扫：`openspec/specs/**/spec.md` 里 strict 校验抓不到的 requirement 正文缺陷。

由来（T-044 评审实证，2026-10-18）：`openspec validate --all --strict` 全绿 ≠ 正文无缺陷——
① 拆条时把一句话从中间切断（"腰斩"）strict 不报；② 把"移动"写成"复制"导致同一句重复，
strict 同样不报。两条都在本轮真实发生过，故此检查落成机器门禁，挂进 `tools/verify.ps1`。

判据（任一命中即 exit 1，逐条打印 文件:行号）：
1. 腰斩：requirement 正文首个非空行以标点开头；
2. 残句：正文末个非空行不以「。；」收尾，且不是"冒号＋紧随列表"形态；
3. 重复句：同一 spec 内 requirement 正文里，长度 ≥15 的分句完全重复出现。
   （阈值取 15 的实证：跨条**有意**复述的短规则句「缺失行任何日期都照常生成」13 字不报；
    本轮真实复制缺陷「两种格式的任务集合 MUST 一致」18 字命中。）

用法：`.venv\\Scripts\\python.exe tools/check_spec_text.py`

已知边界（评审 2026-10-18 登记，未覆盖不算漏判，属"待扩"而非"已保证"）：
- 只扫 `openspec/specs/**/spec.md`（已归档规格库），**不含 `openspec/changes/**` 的在途与归档 delta**——delta 被腰斩/复制时本检查不报；
- 重复句只抓以 。； 切分的 ≥15 字分句，句中被 ，、 分隔的重复不抓；
- 不查语义等价（设计如此：机械检查只管形态）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "openspec" / "specs"
LEADING_PUNCT = "，、；：,。！？"
CLAUSE_SPLIT = re.compile(r"[。；]")


def requirements_of(path: Path):
    """产出 (标题行号, 标题, 正文行列表)。正文＝标题行之后到首个 Scenario 或下一条 requirement 之前。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    idx = [i for i, l in enumerate(lines) if l.startswith("### Requirement: ")]
    for n, start in enumerate(idx):
        end = idx[n + 1] if n + 1 < len(idx) else len(lines)
        body = []
        for line in lines[start + 1:end]:
            if line.startswith("#### Scenario:"):
                break
            body.append(line)
        yield start + 1, lines[start][len("### Requirement: "):], body


def main() -> int:
    problems: list[str] = []
    for path in sorted(SPECS.rglob("spec.md")):
        rel = path.relative_to(ROOT)
        for line_no, title, body in requirements_of(path):
            filled = [(line_no + 1 + i, l.strip()) for i, l in enumerate(body) if l.strip()]
            if not filled:
                problems.append(f"{rel}:{line_no} 条「{title}」正文为空")
                continue
            first_no, first = filled[0]
            if first.startswith(tuple(LEADING_PUNCT)):
                problems.append(f"{rel}:{first_no} 条「{title}」正文以标点开头（疑拆条腰斩）：{first[:24]}")
            last_no, last = filled[-1]
            has_list = any(l.startswith(("- ", "* ")) for _, l in filled)
            if not last.endswith(("。", "；", "。", "；")) and not (last.endswith("：") and has_list):
                problems.append(f"{rel}:{last_no} 条「{title}」正文末行缺句末标点（疑残句）：{last[-24:]}")

        # 重复句：同一 spec 内 requirement 正文的分句重复
        seen: dict[str, str] = {}
        for line_no, title, body in requirements_of(path):
            text = " ".join(l.strip() for l in body if l.strip() and not l.strip().startswith(("- ", "* ")))
            for clause in CLAUSE_SPLIT.split(text):
                clause = clause.strip()
                if len(clause) < 15:
                    continue
                if clause in seen:
                    problems.append(f"{rel}:{line_no} 条「{title}」正文出现重复分句（先见于「{seen[clause]}」）：{clause[:32]}")
                else:
                    seen[clause] = title

    if problems:
        print(f"规范文本复扫未通过：{len(problems)} 处")
        for p in problems:
            print("  -", p)
        return 1
    print(f"规范文本复扫通过：{len(list(SPECS.rglob('spec.md')))} 份 spec，腰斩/残句/重复句均 0 处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
