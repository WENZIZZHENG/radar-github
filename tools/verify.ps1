# 统一验证入口：lint + 规范文本复扫 + 单测一次跑绿才算收口（dev-sop 完成定义：收口只认本入口一次跑绿）
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"   # 中文输出经 PowerShell 管道会按控制台代码页转义成乱码（证据日志不可读），钉死 UTF-8
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    throw "未找到 .venv，先执行: uv venv .venv && uv pip install -r pyproject.toml --extra dev"
}
& $Py -m ruff check app tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Py tools\check_spec_text.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Py -m pytest -q
exit $LASTEXITCODE
