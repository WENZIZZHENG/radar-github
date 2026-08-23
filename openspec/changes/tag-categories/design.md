# 设计：标签分类（tag-categories）

## Context

现状：标签只有一张 `tags(repo_id, tag, PRIMARY KEY(repo_id, tag))` 表——标签不是独立实体，"全部标签"是 `SELECT tag, COUNT(*) FROM tags GROUP BY tag` 的去重视图（routes.py `_tag_cloud`）。/tags 总页平铺标签云；打标交互（T-035 自绘下拉）、/tags/<tag> 结果页、榜单行 chips 均已验收。

约束：S 档单人项目；fail-loud 既定姿态；本人已拍板多对多（一标签可属多分类）；管理操作全部点选式（手机可用，不拖拽）。

## Goals / Non-Goals

**Goals:**
- `tag_category` 多对多映射表；无映射＝未分类。
- /tags 总页分组展示：分类组（组内使用次数降序）＋底部未分类组。
- 分类管理四操作全部点选式：新建、归类（加入/移出）、改名、删除（级联删映射，不动标签与打标记录）。

**Non-Goals:**
- 打标流程不写分类；新标签不做"打标时顺手归类"（天然落未分类，已拍板）。
- 分类不进任何其他页面/链路：榜单、P6 筛选、打标下拉建议、/tags/<tag> 结果页、AI 一概不变。
- 不做拖拽排序、不做分类自定义排序、不做分类图标/颜色。

## Decisions

### 决策 1：映射表存标签字符串，不设外键
`tag_category(tag TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY(tag, category))`。tags 表 tag 列不唯一（按 repo_id+tag 主键），无法做外键目标；标签身份即字符串本身（全项目既有口径，/tags/<tag> 路由同理）。标签重命名不存在（打标侧无改名功能），删除某仓打标不影响其他仓同名标签——映射是否悬空由"标签是否还存在于 tags 去重集合"判定，展示层装配时以 tags 去重集合为准 JOIN 映射，悬空映射行自然不显示（不触发 fail-loud：它是无害残留，随分类删除或改名被清理）。
备选：建独立 tag 实体表重构——否决，动打标主链路，收益为零。

### 决策 2：分组装配在 routes 层，一条查询＋内存分组
`SELECT tag, COUNT(*) AS n FROM tags GROUP BY tag`（既有）＋`SELECT tag, category FROM tag_category`，内存装配：分类组按分类名字典序，组内按 (-n, tag) 降序（沿用既有标签云口径）；无映射标签进"未分类"组，恒在底部。全部无分类（映射表空）时退化为现状平铺云，不渲染"未分类"组头——零分类配置下页面形态与本变更前一致。

### 决策 3：空分类用 `tag=''` 占位行表达
本人拍板表结构为单表 `tag_category(tag, category)` 纯映射，但"新建了分类还没归类任何标签"必须可表达（否则空分类无法存在、归类下拉无数据源）。方案：新建分类写入 `(tag='', category=…)` 占位行——`''` 不可能成为真实标签（打标校验最小 1 字符，`_parse_tag` 兜底）；装配分组与使用次数统计时 `tag=''` 行一律跳过；改名/删除对占位行一视同仁。
备选：独立 categories 表——否决，本人已拍板单表结构，占位行是该约束下的最小代价。

### 决策 4：管理 API 契约（校验/幂等/错误码对齐既有 /api/tags 风格）
- `POST /api/tag-categories` `{"category": "..."}` → 新建（写占位行；同名幂等 `created=false`）；
- `POST /api/tag-categories/rename` `{"from": "...", "to": "..."}` → 改名（全部映射行 UPDATE；目标名已存在 → 合并映射去重；from 不存在 → 404）；
- `DELETE /api/tag-categories?category=...` → 删除分类（含占位行级联全删；不存在幂等 `removed=false`）；
- `POST /api/tag-categories/members` `{"category": "...", "tag": "..."}` → 加入（幂等 `added=false`；tag 不在 tags 去重集合 → 404；category 不存在 → 404）；
- `DELETE /api/tag-categories/members?category=...&tag=...` → 移出（幂等 `removed=false`）。
校验：category 与 tag 同打标约束（去首尾空格、1~20 字符，复用 `_parse_tag` 同款逻辑）。category 含 `/` 等 URL 敏感字符不影响——全部走 JSON body/query 参数（沿用 api_delete_tag "tag 走 query" 的既有形态决策）。

### 决策 5：/tags 总页管理控件形态（全点选式，复用 T-035 组件形态）
- **新建分类**：页头"＋新建分类"按钮 → 展开输入框（打标输入框同款形态：回车提交/Escape 还原/非法红边内联提示）；
- **归类**：每个标签 chip 旁小钮 → 自绘下拉列出全部分类（多选：已属分类打勾）→ 点选即切换加入/移出（点一下加入、再点一下移出），下拉复用 T-035 `tag-suggestions` 组件形态；
- **改名**：分类组头改名小钮 → 输入框形态同上；
- **删除**：分类组头删除小钮 → 一次点击即时删除＋toast（无二次确认——删除只清映射、标签与打标记录不动，可逆代价低，与"× 删除标签""取消关注"均无确认的既有口径一致）。
管理控件只出现在 /tags 总页；/tags/<tag> 结果页不变。每次管理操作成功后整页刷新（SSR 重渲染分组结构，S 档不做局部 DOM 拼装——与打标后 chip 局部更新不同，分组结构变化大，整页刷新更简单可靠）。

### 决策 6：删除分类的级联边界（钉死）
删除分类＝`DELETE FROM tag_category WHERE category=?`（含占位行）。**不做**：删 tags 表任何行、动仓上打标记录、动 /tags/<tag> 结果页。其下标签全部回落未分类组。

## Risks / Trade-offs

- [`tag=''` 占位行是丑招] → 本人拍板单表结构的直接代价；有打标校验（≥1 字符）与装配层过滤双重兜底，写入方仅新建分类一处；改名/删除对占位行一视同仁。留痕待本人确认时知悉。
- [映射悬空（标签最后一条打标被删，映射残留）] → 装配以 tags 去重集合为准 JOIN，悬空行不显示；无 fail-loud（无害残留）。可在删除分类/改名时自然清理；不做主动 GC。
- [改名目标已存在的合并语义] → UPDATE 撞 PRIMARY KEY 行按 `INSERT OR IGNORE` 等价语义去重合并（先 INSERT OR IGNORE 到新名、再删旧名行），不报错——单人场景合并即直觉。
- [生产部署] → schema.sql `CREATE TABLE IF NOT EXISTS` 幂等，服务启动自动建表；存量标签全落未分类，零迁移脚本。

## Migration Plan

1. `schema.sql` 加 `tag_category` 表（IF NOT EXISTS）；
2. 代码部署 → `systemctl restart radar`（启动建表）；
3. A 级预演：真实库建表/新建/归类/改名/删除级联/未分类回落逐项过；
4. 真实页面截图交付本人走查。
回滚：代码回滚重启；`tag_category` 表留库无害（无其他读写方）。

## Open Questions

- 各管理控件的精确文案（"＋新建分类"/归类钮图标或文字/组头钮形态）与"未分类"组头文案 → 《交互流程说明》文字层确认时定稿（本设计只钉行为口径）。
