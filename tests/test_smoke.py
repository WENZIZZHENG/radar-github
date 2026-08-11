from fastapi.testclient import TestClient

from app.main import app


def test_index_ok(tmp_path, monkeypatch):
    # T-017：接口变化后真实库为旧 recommendations 结构，测试不再碰真实库——
    # 改为 tmp 独立库并走 with 上下文触发 lifespan init_db（新结构建表），断言语义不变
    monkeypatch.setenv("RADAR_DB_PATH", str(tmp_path / "smoke.db"))
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "GitHub" in resp.text
