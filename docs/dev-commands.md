# 开发命令手册

> 本项目日常开发与验证命令的唯一登记处，均已实测跑通（Windows + Git Bash 环境）。
> 新增或变更命令必须实际跑通后才写入本文件；统一收口验证入口见 §1，收口只认它一次跑绿。

## 1. 统一验证入口（ruff + pytest）

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools/verify.ps1
```

## 2. 本地起服务

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

## 3. 依赖安装

```bash
uv venv .venv && uv pip install -r pyproject.toml --extra dev
```
