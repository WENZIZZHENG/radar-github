"""T-010 标签功能测试：标签 API（增删/幂等/校验/中文往返）＋ P5 两页（标签云/结果页/空态/导航 active/徽标）。

TestClient 打真实 app（lifespan 会跑 init_db），tmp_path 独立库（RADAR_DB_PATH 覆盖）＋调度关闭，
不碰 data/radar.db（与 tests/test_web.py 同口径）。

标签名原样存储原样判重（大小写敏感）；URL 编码口径：服务端 quote safe=''（空格→%20），
测试断言同样用 urllib.parse.quote 生成期望值，不写死百分号序列。
"""

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn, init_db
from app.main import app

TAG_CN = "选型观察"


def _add_repo(conn, name, *, language=None, stars=100):
    """插一个仓库＋一张快照（标签页按最新快照总星降序，单张快照即端点值）。"""
    cur = conn.execute(
        "INSERT INTO repos (full_name, node_id, description_en, description_zh, language, topics, dead, source, created_at)"
        " VALUES (?, ?, ?, ?, ?, '[]', 0, 'test', '2026-08-09T00:00:00Z')",
        (name, f"node-{name}", f"{name} desc", None, language),
    )
    repo_id = cur.lastrowid
    conn.execute(
        "INSERT INTO star_snapshots (repo_id, captured_at, stars) VALUES (?, '2026-08-09T00:00:00Z', ?)",
        (repo_id, stars),
    )
    return repo_id


def _seed(conn):
    """三仓一关注：a/rs 600 星 > a/py 400 > a/go 200；a/go 已关注（★ 态断言用）。"""
    _add_repo(conn, "a/py", language="Python", stars=400)
    repo_go = _add_repo(conn, "a/go", language="Go", stars=200)
    _add_repo(conn, "a/rs", language="Rust", stars=600)
    conn.execute("INSERT INTO follows (repo_id, created_at) VALUES (?, '2026-08-09T00:00:00Z')", (repo_go,))
    conn.commit()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "tags.db"
    init_db(db)
    conn = get_conn(db)
    _seed(conn)
    conn.close()
    # get_settings 每次调 getenv 无缓存，RADAR_DB_PATH 请求级生效；调度关闭（tests/test_web.py 同口径）
    monkeypatch.setenv("RADAR_DB_PATH", str(db))
    monkeypatch.setenv("RADAR_JOBS_ENABLED", "0")
    with TestClient(app) as c:
        yield c


# ---------- 标签 API ----------


def test_add_tag_and_duplicate_idempotent(client):
    """增标签 200＋响应带该仓全部标签；重名幂等 added=false（不重复写入，对应 toast"标签已存在"）。"""
    resp = client.post("/api/tags", json={"full_name": "a/py", "tag": TAG_CN})
    assert resp.status_code == 200
    data = resp.json()
    assert data["added"] is True
    assert data["tags"] == [TAG_CN]
    resp2 = client.post("/api/tags", json={"full_name": "a/py", "tag": TAG_CN})
    assert resp2.status_code == 200
    assert resp2.json()["added"] is False
    assert resp2.json()["tags"] == [TAG_CN]  # 库内零重复
    # 不同仓库同名标签互不影响
    resp3 = client.post("/api/tags", json={"full_name": "a/go", "tag": TAG_CN})
    assert resp3.json()["added"] is True


def test_tag_strip_and_case_sensitive(client):
    """去首尾空格后判重；大小写敏感（AI 与 ai 视为不同标签，原样存储）。"""
    r1 = client.post("/api/tags", json={"full_name": "a/py", "tag": f"  {TAG_CN} "})
    assert r1.json()["added"] is True
    assert r1.json()["tags"] == [TAG_CN]  # 存的是去空格后原样
    r2 = client.post("/api/tags", json={"full_name": "a/py", "tag": TAG_CN})
    assert r2.json()["added"] is False  # 去空格后重名 → 幂等
    client.post("/api/tags", json={"full_name": "a/py", "tag": "AI"})
    r3 = client.post("/api/tags", json={"full_name": "a/py", "tag": "ai"})
    assert r3.json()["added"] is True
    assert r3.json()["tags"] == ["AI", "ai", TAG_CN]  # ORDER BY tag：ASCII 大写 < 小写 < 中文 UTF-8 字节


def test_add_tag_validation(client):
    """服务端兜底校验：空/纯空格/超 20 字符/非法 full_name/非 dict body → 400；仓库不在池 → 404。"""
    assert client.post("/api/tags", json={"full_name": "a/py", "tag": ""}).status_code == 400
    assert client.post("/api/tags", json={"full_name": "a/py", "tag": "   "}).status_code == 400
    assert client.post("/api/tags", json={"full_name": "a/py", "tag": "x" * 21}).status_code == 400
    assert client.post("/api/tags", json={"full_name": "nope", "tag": "ok"}).status_code == 400
    assert client.post("/api/tags", json={"full_name": "a/py", "tag": 123}).status_code == 400
    assert client.post("/api/tags", json=[1, 2]).status_code == 400
    assert client.post("/api/tags", json={"full_name": "no/such", "tag": "ok"}).status_code == 404


def test_delete_tag_idempotent(client):
    """删标签 removed=true；再删/删不存在/删不存在仓库 → removed=false 幂等；缺 tag 参数 → 400。"""
    client.post("/api/tags", json={"full_name": "a/py", "tag": TAG_CN})
    resp = client.delete(f"/api/tags/a/py?tag={quote(TAG_CN)}")
    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    assert client.delete(f"/api/tags/a/py?tag={quote(TAG_CN)}").json()["removed"] is False
    assert client.delete(f"/api/tags/a/py?tag={quote('不存在')}").json()["removed"] is False
    # 仓库不在池：幂等成功（与取消关注同口径，删除目标本就不存在）
    assert client.delete("/api/tags/no/such?tag=x").json()["removed"] is False
    assert client.delete("/api/tags/a/py").status_code == 400


def test_chinese_tag_roundtrip(client):
    """中文＋空格标签：POST → 结果页 URL 往返（quote safe='' 编码）→ DELETE，全程原样。"""
    tag = "中文 标签"  # 含空格：quote safe='' 编码为 %20，FastAPI :path 解码还原
    client.post("/api/tags", json={"full_name": "a/py", "tag": tag})
    page = client.get(f"/tags/{quote(tag, safe='')}")
    assert page.status_code == 200
    assert tag in page.text
    assert "a/py" in page.text
    assert client.delete(f"/api/tags/a/py?tag={quote(tag)}").json()["removed"] is True


def test_slash_tag_roundtrip(client):
    """含 / 标签（a/b 型）：quote safe='' 编码为 %2F，:path 贪婪匹配＋解码还原，全流程往返。"""
    tag = "前端/构建"
    client.post("/api/tags", json={"full_name": "a/py", "tag": tag})
    page = client.get(f"/tags/{quote(tag, safe='')}")
    assert page.status_code == 200
    assert tag in page.text
    assert "a/py" in page.text
    assert client.delete(f"/api/tags/a/py?tag={quote(tag, safe='')}").json()["removed"] is True


# ---------- P5 页面 ----------


def _seed_cloud(client):
    """两仓打"选型观察"、a/py 另打"AI"：标签云项目数 2/1；结果页总星序 a/py(400) > a/go(200)，a/py 行内两枚 chip。"""
    for repo in ("a/py", "a/go"):
        client.post("/api/tags", json={"full_name": repo, "tag": TAG_CN})
    client.post("/api/tags", json={"full_name": "a/py", "tag": "AI"})


def test_tags_cloud_page(client):
    """P5 总页：导航"标签" active＋follow_count 注入；标签＋项目数；项目数降序、同数按标签名。"""
    _seed_cloud(client)
    resp = client.get("/tags")
    assert resp.status_code == 200
    text = resp.text
    assert '<a href="/tags" class="active" aria-current="page">标签</a>' in text
    assert 'id="nav-follow-count">1</i>' in text  # _seed 有一条关注（a/go）
    assert f'<a class="tag tag-cloud" href="/tags/{quote(TAG_CN, safe="")}">{TAG_CN}<i>2</i></a>' in text
    assert f'<a class="tag tag-cloud" href="/tags/{quote("AI", safe="")}">AI<i>1</i></a>' in text
    # 排序：项目数降序（选型观察 2 在 AI 1 前）
    assert text.index(f">{TAG_CN}<i>2</i>") < text.index(">AI<i>1</i>")


def test_tags_cloud_empty_state(client):
    """空库空态：引导文案＋回首页链接；无标签 chip。"""
    resp = client.get("/tags")
    assert resp.status_code == 200
    assert "还没有任何标签" in resp.text
    assert '<a href="/">回首页</a>' in resp.text
    assert "tag-cloud" not in resp.text


def test_tag_page_rows_and_controls(client):
    """P5 结果页：行渲染（按总星降序）、默认展开、★ 态、chip 链接（SSR 编码）＋× 删除钮＋tag-add 按钮。"""
    _seed_cloud(client)
    resp = client.get(f"/tags/{quote(TAG_CN, safe='')}")
    assert resp.status_code == 200
    text = resp.text
    assert f"标签「{TAG_CN}」" in text
    assert text.index("a/py") < text.index("a/go")  # 400 星 > 200 星
    assert "a/rs" not in text  # 不在此标签下
    assert 'class="row expanded"' in text and 'class="panel open"' in text  # 紧凑行＋默认展开
    assert 'class="star on" data-repo="a/go"' in text  # 已关注 → 实心★
    assert 'data-repo="a/py"' in text  # 未关注星标（JS 关注接线）
    # 行内面板：chip 链接＋× 删除钮＋"＋ 标签"按钮（四页共用 _row.html）
    assert f'<a class="tag-link" href="/tags/{quote("AI", safe="")}">AI</a>' in text  # a/py 另有 AI 标签
    assert '<b title="删除标签" data-repo="a/py" data-tag="AI">×</b>' in text
    assert f'<b title="删除标签" data-repo="a/py" data-tag="{TAG_CN}">×</b>' in text
    assert 'class="tag-add" data-repo="a/py"' in text
    assert 'class="tag-add" data-repo="a/go"' in text


def test_tag_page_empty_state(client):
    """空标签筛选空态（流程说明 §4）："还没有项目打过「xx」标签"＋返回 /tags 链接。"""
    resp = client.get(f"/tags/{quote('不存在', safe='')}")
    assert resp.status_code == 200
    assert "还没有项目打过「不存在」标签" in resp.text
    assert '<a href="/tags">返回标签列表</a>' in resp.text


def test_row_tag_controls_on_all_pages(client):
    """行内增删控件（tag-add 按钮＋chip 链接）在 P1/P3/P4/P6 四页全部就位（共用 _row.html）。"""
    _seed_cloud(client)
    for path in ("/", "/quarter", "/total"):
        text = client.get(path).text
        assert 'class="tag-add" data-repo="a/py"' in text, path
        assert f'<a class="tag-link" href="/tags/{quote(TAG_CN, safe="")}">{TAG_CN}</a>' in text, path
    # P6 只有关注仓库（a/go）：该行同样带标签区增删控件
    text = client.get("/follows").text
    assert 'class="tag-add" data-repo="a/go"' in text
    assert f'<a class="tag-link" href="/tags/{quote(TAG_CN, safe="")}">{TAG_CN}</a>' in text


def test_tags_persist_after_refresh(client):
    """刷新后仍在：打标后重新 GET 页面断言 chip SSR 持久；删除后刷新不再出现。"""
    client.post("/api/tags", json={"full_name": "a/py", "tag": TAG_CN})
    total = client.get("/total")
    assert f'<span class="tag"><a class="tag-link" href="/tags/{quote(TAG_CN, safe="")}">{TAG_CN}</a>' in total.text
    assert 'class="tag-add" data-repo="a/py"' in total.text
    client.delete(f"/api/tags/a/py?tag={quote(TAG_CN)}")
    after = client.get("/total")
    assert TAG_CN not in after.text
