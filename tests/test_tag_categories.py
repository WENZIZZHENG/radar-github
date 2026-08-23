"""T-039 标签分类测试（§19）：tag_category 多对多映射表、/tags 分组展示、分类管理四操作 API。

TestClient 打真实 app（lifespan 跑 init_db），tmp_path 独立库（RADAR_DB_PATH 覆盖）＋调度关闭，
不 mock 内部 DB（与 tests/test_tags.py 同口径）。

口径锚点：
- 建表幂等（schema.sql 全量 IF NOT EXISTS，init_db 重复执行安全）；
- 空分类＝tag='' 占位行，装配与使用次数统计跳过；悬空映射（标签打标记录已删）不显示不报错；
- 分组：分类组字典序、组内 (-n, tag)、底部"未分类"组、一标签多分类各组重复出现、零分类退化平铺；
- 四操作幂等/校验/404 对齐既有 /api/tags；删除分类只清映射（tags 表与打标记录不动）。
"""

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn, init_db
from app.main import app


def _add_repo(conn, name, *, language=None, stars=100):
    """插一个仓库＋一张快照（标签页按最新快照总星降序，单张快照即端点值）。"""
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at)"
        " VALUES (?, ?, ?, NULL, ?, '[]', 0, 'test', '2026-08-09T00:00:00Z')",
        (name, f"node-{name}", f"{name} desc", language),
    )
    repo_id = cur.lastrowid
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, '2026-08-09T00:00:00Z', ?)",
        (repo_id, stars),
    )
    return repo_id


def _seed(conn):
    """三仓：a/py 400、a/go 200、a/rs 600。"""
    _add_repo(conn, "a/py", language="Python", stars=400)
    _add_repo(conn, "a/go", language="Go", stars=200)
    _add_repo(conn, "a/rs", language="Rust", stars=600)
    conn.commit()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """产出 (TestClient, db_path)：db_path 供级联边界/占位行等 DB 级断言直查。"""
    db = tmp_path / "tagcat.db"
    init_db(db)
    conn = get_conn(db)
    _seed(conn)
    conn.close()
    # get_settings 每次调 getenv 无缓存，RADAR_DB_PATH 请求级生效；调度关闭（tests/test_tags.py 同口径）
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    with TestClient(app) as c:
        yield c, db


def _tag_rows(db):
    """tag_category 全表行（(tag, category) 列表，ORDER BY 稳定）。"""
    conn = get_conn(db)
    try:
        return [(r["tag"], r["category"]) for r in conn.execute("SELECT tag, category FROM tag_category ORDER BY tag, category")]
    finally:
        conn.close()


def _tags_table_count(db):
    conn = get_conn(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    finally:
        conn.close()


# ---------- 建表幂等 ----------


def test_init_db_creates_tag_category_idempotent(tmp_path):
    """schema.sql 全量 IF NOT EXISTS：init_db 重复执行安全，tag_category 表结构钉死。"""
    db = tmp_path / "init.db"
    init_db(db)
    init_db(db)  # 重复执行不报错
    conn = get_conn(db)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tag_category)")}
        assert cols == {"tag", "category"}
    finally:
        conn.close()


# ---------- 新建分类 ----------


def test_create_category_and_idempotent(client):
    c, db = client
    resp = c.post("/api/tag-categories", json={"category": "工具"})
    assert resp.status_code == 200
    assert resp.json() == {"created": True, "category": "工具"}
    assert _tag_rows(db) == [("", "工具")]  # 占位行表达空分类
    # 同名幂等：created=false，库内零变化
    resp2 = c.post("/api/tag-categories", json={"category": " 工具 "})  # 去首尾空格后撞名
    assert resp2.json()["created"] is False
    assert _tag_rows(db) == [("", "工具")]
    # 空分类组渲染：组头在、组内 0 标签
    text = c.get("/tags").text
    assert "工具<i>0</i>" in text


def test_create_category_validation(client):
    """校验同打标约束：空/纯空格/超 20 字符/非 dict body → 400。"""
    c, db = client
    assert c.post("/api/tag-categories", json={"category": ""}).status_code == 400
    assert c.post("/api/tag-categories", json={"category": "   "}).status_code == 400
    assert c.post("/api/tag-categories", json={"category": "x" * 21}).status_code == 400
    assert c.post("/api/tag-categories", json={"category": 123}).status_code == 400
    assert c.post("/api/tag-categories", json=[1, 2]).status_code == 400
    assert _tag_rows(db) == []


# ---------- 归类（加入/移出） ----------


def _add_tag(c, repo, tag):
    assert c.post("/api/tags", json={"full_name": repo, "tag": tag}).json()["added"] is True


def test_assign_member_and_idempotent(client):
    c, db = client
    _add_tag(c, "a/py", "选型观察")
    c.post("/api/tag-categories", json={"category": "工具"})
    resp = c.post("/api/tag-categories/members", json={"category": "工具", "tag": "选型观察"})
    assert resp.status_code == 200
    assert resp.json()["added"] is True
    # 重复加入幂等：added=false，映射不重复
    resp2 = c.post("/api/tag-categories/members", json={"category": "工具", "tag": "选型观察"})
    assert resp2.json()["added"] is False
    assert _tag_rows(db) == [("", "工具"), ("选型观察", "工具")]


def test_assign_member_404(client):
    """归类目标校验：tag 不在 tags 去重集合 → 404；category 不存在 → 404；均无写入。"""
    c, db = client
    _add_tag(c, "a/py", "选型观察")
    c.post("/api/tag-categories", json={"category": "工具"})
    assert c.post("/api/tag-categories/members", json={"category": "工具", "tag": "不存在"}).status_code == 404
    assert c.post("/api/tag-categories/members", json={"category": "不存在", "tag": "选型观察"}).status_code == 404
    assert _tag_rows(db) == [("", "工具")]


def test_remove_member_idempotent(client):
    """移出：removed=true → 再删/删不存在映射/删不存在分类均幂等 removed=false（不 404）。"""
    c, db = client
    _add_tag(c, "a/py", "选型观察")
    c.post("/api/tag-categories", json={"category": "工具"})
    c.post("/api/tag-categories/members", json={"category": "工具", "tag": "选型观察"})
    q = f"category={quote('工具')}&tag={quote('选型观察')}"
    assert c.delete(f"/api/tag-categories/members?{q}").json()["removed"] is True
    assert c.delete(f"/api/tag-categories/members?{q}").json()["removed"] is False
    assert c.delete(f"/api/tag-categories/members?category={quote('不存在')}&tag={quote('选型观察')}").json()["removed"] is False
    assert _tag_rows(db) == [("", "工具")]  # 占位行保留


# ---------- 改名 ----------


def test_rename_category(client):
    """改名：全部映射换名（含占位行）；from 不存在 → 404。"""
    c, db = client
    _add_tag(c, "a/py", "选型观察")
    c.post("/api/tag-categories", json={"category": "工具"})
    c.post("/api/tag-categories/members", json={"category": "工具", "tag": "选型观察"})
    resp = c.post("/api/tag-categories/rename", json={"from": "工具", "to": "利器"})
    assert resp.status_code == 200
    assert resp.json()["renamed"] is True
    assert _tag_rows(db) == [("", "利器"), ("选型观察", "利器")]
    assert c.post("/api/tag-categories/rename", json={"from": "工具", "to": "x"}).status_code == 404


def test_rename_merge_dedup(client):
    """撞名合并：改名为既有分类 → 映射并入去重不报错；同名改名幂等（不清空自己）。"""
    c, db = client
    _add_tag(c, "a/py", "选型观察")
    _add_tag(c, "a/go", "AI")
    c.post("/api/tag-categories", json={"category": "工具"})
    c.post("/api/tag-categories", json={"category": "利器"})
    c.post("/api/tag-categories/members", json={"category": "工具", "tag": "选型观察"})
    c.post("/api/tag-categories/members", json={"category": "工具", "tag": "AI"})
    c.post("/api/tag-categories/members", json={"category": "利器", "tag": "选型观察"})  # 撞名后重复映射
    resp = c.post("/api/tag-categories/rename", json={"from": "工具", "to": "利器"})
    assert resp.status_code == 200
    # 合并去重：利器 = 占位行＋选型观察＋AI（选型观察不重复），工具不再存在
    assert _tag_rows(db) == [("", "利器"), ("AI", "利器"), ("选型观察", "利器")]
    # 同名改名：幂等无操作（防御合并语句清空自己）
    assert c.post("/api/tag-categories/rename", json={"from": "利器", "to": "利器"}).json()["renamed"] is True
    assert _tag_rows(db) == [("", "利器"), ("AI", "利器"), ("选型观察", "利器")]


# ---------- 删除分类与级联边界 ----------


def test_delete_category_cascade_boundary(client):
    """删除分类：只删 tag_category 该分类全部行（含占位行）；tags 表与打标记录不动；标签回落未分类。"""
    c, db = client
    _add_tag(c, "a/py", "选型观察")
    _add_tag(c, "a/go", "选型观察")
    c.post("/api/tag-categories", json={"category": "工具"})
    c.post("/api/tag-categories", json={"category": "保留"})  # 幸存分类：删除后仍处分组视图（不归零退化）
    c.post("/api/tag-categories/members", json={"category": "工具", "tag": "选型观察"})
    tags_before = _tags_table_count(db)
    resp = c.delete(f"/api/tag-categories?category={quote('工具')}")
    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    assert _tag_rows(db) == [("", "保留")]  # 该分类占位行＋映射全删，其他分类不动
    assert _tags_table_count(db) == tags_before  # tags 表行数不变（级联边界钉死）
    # 标签回落未分类组；/tags/<tag> 结果页不受影响
    text = c.get("/tags").text
    assert "未分类" in text and "选型观察" in text
    assert "工具<i>" not in text
    page = c.get(f"/tags/{quote('选型观察', safe='')}")
    assert page.status_code == 200
    assert "a/py" in page.text and "a/go" in page.text
    # 不存在幂等 removed=false
    assert c.delete(f"/api/tag-categories?category={quote('工具')}").json()["removed"] is False


# ---------- 分组装配 ----------


def _seed_grouped(c):
    """两分类三标签：选型观察 n=2 同属"工具"与"想读源码"；AI n=1 属"工具"；未归类 n=1 无映射。"""
    for repo in ("a/py", "a/go"):
        _add_tag(c, repo, "选型观察")
    _add_tag(c, "a/py", "AI")
    _add_tag(c, "a/rs", "未归类")
    c.post("/api/tag-categories", json={"category": "想读源码"})
    c.post("/api/tag-categories", json={"category": "工具"})
    c.post("/api/tag-categories/members", json={"category": "工具", "tag": "选型观察"})
    c.post("/api/tag-categories/members", json={"category": "想读源码", "tag": "选型观察"})
    c.post("/api/tag-categories/members", json={"category": "工具", "tag": "AI"})


def test_groups_assembly(client):
    """分组渲染：组间字典序、组内使用次数降序（同数按名）、未分类组垫底、一标签多分类各组重复出现。"""
    c, _ = client
    _seed_grouped(c)
    text = c.get("/tags").text
    # 组头：分类名＋组内标签数
    assert "工具<i>2</i>" in text
    assert "想读源码<i>1</i>" in text
    assert "未分类<i>1</i>" in text
    # 组间顺序：工具 < 想读源码（字典序）→ 未分类垫底
    assert text.index("工具<i>2</i>") < text.index("想读源码<i>1</i>") < text.index("未分类<i>1</i>")
    # 组内排序：选型观察 n=2 在 AI n=1 前
    assert text.index(">选型观察<i>2</i>") < text.index(">AI<i>1</i>")
    # 一标签多分类：选型观察 chip 在两个组各出现一次
    href = f"/tags/{quote('选型观察', safe='')}"
    assert text.count(f'<a class="tag tag-cloud" href="{href}">') == 2
    # 未分类组含未归类标签
    assert '>未归类<i>1</i>' in text
    # 管理控件只在本页就位：页头新建钮、chip 归类钮、组头改名/删除钮
    # （data-cats 经 tojson|forceescape：中文转 \uXXXX、引号转 &#34;，只断言形态不断言具体转义）
    assert 'id="cat-create"' in text
    assert 'class="cat-assign" data-tag="AI" data-cats="[&#34;' in text
    assert 'data-cat-rename="工具"' in text
    assert 'data-cat-delete="工具"' in text
    # 未分类组头无管理钮
    assert "未分类<i>1</i><button" not in text


def test_zero_category_degrades_to_flat(client):
    """零分类退化：无任何分类时页面与平铺标签云现状一致——无分组结构、无"未分类"组头；新建钮仍在。"""
    c, _ = client
    for repo in ("a/py", "a/go"):
        _add_tag(c, repo, "选型观察")
    _add_tag(c, "a/py", "AI")
    text = c.get("/tags").text
    assert "cat-head" not in text
    assert "未分类" not in text
    assert '<h2 class="sec">全部标签</h2>' in text  # 现状平铺节头
    assert f'<a class="tag tag-cloud" href="/tags/{quote("选型观察", safe="")}">选型观察<i>2</i></a>' in text
    assert 'id="cat-create"' in text  # 页头新建钮总页恒在（否则首个分类无入口）


def test_placeholder_and_dangling_mapping_skipped(client):
    """占位行与悬空映射跳过：空分类组头计数 0；标签最后一条打标记录删除后映射残留不显示不报错。"""
    c, db = client
    _add_tag(c, "a/py", "选型观察")
    c.post("/api/tag-categories", json={"category": "工具"})
    c.post("/api/tag-categories/members", json={"category": "工具", "tag": "选型观察"})
    # 删掉该标签唯一打标记录 → 映射悬空
    assert c.delete(f"/api/tags/a/py?tag={quote('选型观察')}").json()["removed"] is True
    assert _tag_rows(db) == [("", "工具"), ("选型观察", "工具")]  # 残留不主动清理
    text = c.get("/tags").text
    assert text.count("工具<i>0</i>") == 1  # 组头计数 0（悬空行不计）
    assert "选型观察" not in text  # 悬空映射不显示
    assert "未分类" not in text  # 无任何未分类标签 → 未分类组不渲染


def test_tagging_does_not_write_category(client):
    """打标流程不写分类：新标签只写 tags 表，天然落未分类组。"""
    c, db = client
    c.post("/api/tag-categories", json={"category": "工具"})
    _add_tag(c, "a/py", "全新标签")
    assert _tag_rows(db) == [("", "工具")]  # tag_category 无新行
    text = c.get("/tags").text
    assert "未分类<i>1</i>" in text
    assert ">全新标签<i>1</i>" in text
