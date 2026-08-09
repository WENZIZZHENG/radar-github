# 统一验证入口：lint + 单测一次跑绿才算收口（dev-sop 完成定义：收口只认本入口一次跑绿）
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    throw "未找到 .venv，先执行: uv venv .venv && uv pip install -r pyproject.toml --extra dev"
}
& $Py -m ruff check app tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Py -m pytest -q
exit $LASTEXITCODE
