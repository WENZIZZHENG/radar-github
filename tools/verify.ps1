# 统一验证入口：lint + 规范文本复扫 + 单测一次跑绿才算收口（dev-sop 完成定义：收口只认本入口一次跑绿）
#
# 两档（本人 2026-10-18 拍板）：
#   不带参数 = 收口档：ruff + 规范文本复扫 + 全量 pytest，一个批次只跑一次；
#   -Fast    = 过程验证档：ruff + 规范文本复扫 + **只跑受影响测试**（按改动文件映射），
#              供开发/修复/评审中间轮次使用；它不构成收口，收口必须不带参数重跑全量。
param([switch]$Fast)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"   # 中文输出经 PowerShell 管道会按控制台代码页转义成乱码（证据日志不可读），钉死 UTF-8
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    throw "未找到 .venv，先执行: uv sync --locked --extra dev"
}

& $Py -m ruff check app tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Py tools\check_spec_text.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($Fast) {
    # 改动文件（已改未暂存 + 已暂存 + 未跟踪）→ 测试文件映射：tests/test_<同名>.py 命中即用。
    # 注意：同名映射是**近似受影响面**，不是依赖闭包；源码改动映射不到同名测试时回落全量，
    # 避免出现「零测试的绿」（本仓 app/web/routes.py、app/main.py、app/collector/discover.py、tests/conftest.py 都没有同名测试）。
    $changed = @()
    $changed += git -C $Root -c core.quotepath=false diff --name-only
    $changed += git -C $Root -c core.quotepath=false diff --name-only --cached
    $changed += git -C $Root -c core.quotepath=false ls-files --others --exclude-standard
    if ($LASTEXITCODE -ne 0) { throw "git 查询改动文件失败（exit $LASTEXITCODE），-Fast 中止以免误判为无改动" }
    $changed = @($changed | Where-Object { $_ } | Sort-Object -Unique)
    $targets = @()
    $map = @{}   # 测试文件 → 映射到它的源码文件（供对照行打印，避免"声称不实"）
    foreach ($f in $changed) {
        if ($f -like "tests/test_*.py") {
            if (Test-Path (Join-Path $Root $f)) { $targets += $f }
            continue
        }
        $stem = [IO.Path]::GetFileNameWithoutExtension($f)
        $guess = "tests/test_$stem.py"
        if (Test-Path (Join-Path $Root $guess)) {
            $targets += $guess
            if (-not $map.ContainsKey($guess)) { $map[$guess] = @() }
            $map[$guess] += $f
        }
    }
    $targets = @($targets | Sort-Object -Unique)
    # 源码改动（含 tools/ 脚本与 conftest 这类共享夹具）只要有一条没映射到测试，就回落全量：
    # 混合改动（部分命中 + 部分未命中）同样回落，避免静默漏测。
    $codeChanged = @($changed | Where-Object { $_ -like "*.py" -or $_ -like "app/web/static/*" -or $_ -like "app/web/templates/*" } | Where-Object { $_ -notlike "tests/test_*.py" })
    $unmapped = @($codeChanged | Where-Object { -not (Test-Path (Join-Path $Root ("tests/test_" + [IO.Path]::GetFileNameWithoutExtension($_) + ".py"))) })
    if ($unmapped.Count -gt 0 -or $codeChanged -contains "tests/conftest.py") {
        Write-Host ("[-Fast] 有源码改动未映射到同名测试或含共享夹具（" + ($unmapped -join ", ") + "），为避免漏测回落全量 pytest；本分支是 -Fast 回落，收口仍需不带参数重跑")
        & $Py -m pytest -q
        exit $LASTEXITCODE
    }
    if ($targets.Count -eq 0) {
        Write-Host "[-Fast] 改动未含源码（纯文档/静态资源），只跑 ruff + 规范文本复扫；收口仍需全量"
        exit 0
    }
    Write-Host "[-Fast] 过程验证档（非收口口径；同名映射＝近似受影响面）："
    foreach ($tg in $targets) {
        if ($map.ContainsKey($tg)) {
            Write-Host ("         " + $tg + " ← " + ($map[$tg] -join " / "))
        } else {
            Write-Host ("         " + $tg + " ← (仅测试文件改动)")
        }
    }
    & $Py -m pytest -q @targets
    exit $LASTEXITCODE
}
# 收口档（不带参数）：全量 pytest，一个批次只跑一次
& $Py -m pytest -q
exit $LASTEXITCODE
