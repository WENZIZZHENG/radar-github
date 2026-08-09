from fastapi.testclient import TestClient

from app.main import app


def test_index_ok():
    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert "GitHub" in resp.text
