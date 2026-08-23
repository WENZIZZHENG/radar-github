# 任务清单：标签分类（tag-categories）

> 流程口径（项目 SOP）：文字层先行——任务 1.1《交互流程说明》更新需本人确认后再派实施；高风险（新表＋改已验收 P5 页面），详情卡必填；A 级预演；视觉为既有组件复用（自绘下拉/输入框/chip），免示意图档，开发完以真实页面截图交付走查。

## 1. 文字层与数据层

- [x] 1.1 更新《交互流程说明》：/tags 分组展示＋分类管理四操作流程（控件形态、文案、空态、级联边界）→ 本人确认
- [x] 1.2 `app/schema.sql`：新增 `tag_category(tag TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY(tag, category))`（IF NOT EXISTS，启动幂等建表；无映射＝未分类；新建分类写 `tag=''` 占位行）

## 2. 服务端（app/web/routes.py）

- [x] 2.1 /tags 总页分组装配：tags 去重集合 JOIN 映射（内存），分类组按分类名字典序、组内 (-n, tag) 降序，无映射标签进底部"未分类"组；映射表空时退化为现状平铺云；`tag=''` 占位行与悬空映射一律跳过
- [x] 2.2 分类管理 API 一组（校验/幂等/404 口径对齐既有 /api/tags）：POST /api/tag-categories 新建（占位行，同名幂等）；POST /api/tag-categories/rename 改名（目标已存在合并去重，from 不存在 404）；DELETE /api/tag-categories?category= 删除（级联删映射含占位行，幂等）；POST/DELETE /api/tag-categories/members 归类加入/移出（tag 不在 tags 去重集合或 category 不存在 → 404，幂等）
- [x] 2.3 删除分类级联边界钉死：只 DELETE tag_category 行，不触碰 tags 表与仓上打标记录

## 3. 页面与交互（app/web/templates/tags.html + radar.js + radar.css）

- [x] 3.1 分组云视图：分类组头（分类名＋组内标签数）＋组内 chips；底部"未分类"组；一标签属多分类在多组各出现一次
- [x] 3.2 管理控件（仅 /tags 总页，全点选式）：页头"＋新建分类"输入框（打标输入框同款形态）；标签 chip 归类钮 → 自绘多选下拉（复用 T-035 `tag-suggestions` 组件形态，已属分类打勾，点选切换）；组头改名钮（输入框）与删除钮（点击即时删＋toast，无二次确认）
- [x] 3.3 管理操作成功后整页刷新重渲染分组结构
- [x] 3.4 确认不动：打标下拉（T-035）、榜单行 chips、/tags/<tag> 结果页零改动

## 4. 测试

- [x] 4.1 新增：分组装配（组序/组内排序/未分类组垫底/多分类重复出现/映射表空退化平铺/占位行与悬空映射跳过）
- [x] 4.2 新增：四类管理 API——新建幂等、归类 404 分支（tag 不存在/category 不存在）、改名合并去重与 404、删除级联只动 tag_category（断言 tags 表行数不变）、校验约束（空/超 20 字符 400）
- [x] 4.3 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1` 一次跑绿

## 5. 评审与交付

- [ ] 5.1 独立评审子 agent，中/高 findings 清零（最多三轮）
- [ ] 5.2 A 级预演（真实库：建表/新建空分类/归类/一标签多分类/改名合并/删除级联＋未分类回落逐项过）＋真实页面截图＋复验步骤卡 → 本人走查
- [ ] 5.3 部署生产（启动自动建表）并冒烟（/tags 分组渲染＋一轮真实 CRUD）
- [ ] 5.4 通过后当场归档（任务拆解表主表行＋详情卡搬归档，openspec archive），文档按 §5 节奏提交
