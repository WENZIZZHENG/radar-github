# 规格：标签分类（tag-categories）

## Purpose

提供标签分类（P11）：tag_category 多对多映射表＋/tags 分组展示＋点选式分类管理（新建/归类/改名/删除级联只清映射）。本规格由 change `tag-categories` 归档沉淀（2026-08-23）。

## Requirements

### Requirement: tag_category 多对多映射表
系统 SHALL 新增 `tag_category(tag TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY(tag, category))` 表表达标签↔分类多对多映射（一标签可属多分类，一分类可含多标签）。无任何映射记录的标签＝未分类。打标流程（POST /api/tags 及行内交互）MUST NOT 写该表——新标签天然落未分类。新建分类时系统 SHALL 写入 `(tag='', category=…)` 占位行以表达空分类；`tag=''` 行 MUST 在分组装配与使用次数统计中一律跳过（打标校验最小 1 字符，真实标签不可能为空串）。

#### Scenario: 打标不写分类
- **WHEN** 用户给仓库打一个全新标签
- **THEN** 只写 tags 表，tag_category 无任何新行；该标签出现在 /tags "未分类"组

#### Scenario: 新建空分类
- **WHEN** 用户新建分类"工具"且尚未归类任何标签
- **THEN** tag_category 存在 `('', '工具')` 占位行，/tags 渲染"工具"空组（组头在、组内无标签）

### Requirement: /tags 分组展示口径
/tags 总页 SHALL 按分组渲染标签云：每个分类一组，组头为分类名＋组内标签数；分类组按分类名字典序排列，组内标签按使用次数降序、同数按标签名（沿用既有标签云口径）；无映射标签进"未分类"组，恒在底部；一标签属多分类时在多个组各出现一次（分组语义）。映射表为空（无任何分类）时 MUST 退化为平铺标签云，不渲染分组结构与"未分类"组头。装配 MUST 以 tags 表去重集合为准 JOIN 映射——悬空映射行（标签已无任何打标记录）不显示，不报错。/tags/<tag> 结果页 MUST NOT 改变。

#### Scenario: 分组渲染
- **WHEN** 存在分类"工具"（含标签 a、b）与未分类标签 c
- **THEN** 页面先渲染"工具"组（a、b 按使用次数降序），底部"未分类"组含 c

#### Scenario: 一标签多分类
- **WHEN** 标签 a 同属"工具"与"想读源码"两个分类
- **THEN** a 在两个组各出现一次

#### Scenario: 零分类退化
- **WHEN** tag_category 全空
- **THEN** /tags 与本变更前一致：平铺标签云，无组头

### Requirement: 分类管理 API
系统 SHALL 提供一组管理 API，校验（去首尾空格、1~20 字符，同打标约束）、幂等与错误码口径 MUST 对齐既有 /api/tags 风格：
- `POST /api/tag-categories`：新建分类（写占位行；同名幂等 `created=false`）；
- `POST /api/tag-categories/rename`：改名（from 不存在 → 404；目标名已存在 → 合并映射并去重，不报错）；
- `DELETE /api/tag-categories?category=...`：删除分类（不存在幂等 `removed=false`）；
- `POST /api/tag-categories/members`：标签加入分类（幂等 `added=false`；tag 不在 tags 去重集合 → 404；category 不存在 → 404）；
- `DELETE /api/tag-categories/members?category=...&tag=...`：移出（幂等 `removed=false`）。

#### Scenario: 重复加入幂等
- **WHEN** 把已属"工具"的标签 a 再次加入"工具"
- **THEN** 返回 `added=false`，映射不重复

#### Scenario: 改名撞名合并
- **WHEN** 把分类"工具"改名为已存在的"利器"
- **THEN** "工具"下全部映射并入"利器"（重复映射去重），"工具"不再存在

#### Scenario: 归类不存在的标签
- **WHEN** 把 tags 去重集合中不存在的标签加入某分类
- **THEN** 返回 404，无写入

### Requirement: 删除分类的级联边界
删除分类 SHALL 仅删除 tag_category 中该分类的全部行（含占位行），其下标签回落未分类；tags 表与该仓上的打标记录 MUST NOT 有任何变化。

#### Scenario: 删除分类标签回落
- **WHEN** 删除分类"工具"（其下标签 a、b 均有仓上打标记录）
- **THEN** tag_category 中"工具"行全删；a、b 出现在"未分类"组；tags 表行数不变，/tags/a 结果页内容不变

### Requirement: 管理控件点选式形态
/tags 总页 SHALL 提供分类管理控件（仅总页，全点选式、手机可用、无拖拽）：页头"＋新建分类"按钮展开输入框（打标输入框同款形态：回车提交、Escape 还原、非法输入红边内联提示）；每个标签 chip 旁归类钮点开自绘多选下拉（复用 T-035 `tag-suggestions` 组件形态），列出全部分类、已属分类打勾，点选即切换加入/移出；分类组头提供改名钮（输入框形态）与删除钮（点击即时删除＋toast，无二次确认）。管理操作成功后 SHALL 整页刷新重渲染分组结构。

#### Scenario: 点选归类
- **WHEN** 用户在标签 a 的归类下拉中点选未勾选的"工具"
- **THEN** a 加入"工具"；再点一次移出

#### Scenario: 删除即时生效
- **WHEN** 用户点分类组头删除钮
- **THEN** 分类即时删除＋toast，页面刷新后该组消失、其下标签落入未分类组
