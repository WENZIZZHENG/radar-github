from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="GitHub 雷达")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """首页占位：证明服务可达；T-008 榜单页面落地后替换本页。"""
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>GitHub 雷达</title></head><body>"
        "<h1>GitHub 雷达运行中</h1>"
        "<p>榜单功能开发中（T-008 后替换本页）。</p>"
        "</body></html>"
    )
