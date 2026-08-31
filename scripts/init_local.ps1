$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "未找到 .venv，请先创建虚拟环境并安装 requirements-dev.txt"
}

Push-Location $projectRoot
try {
    & $pythonPath -m alembic upgrade head
    & $pythonPath -m scripts.seed_demo
} finally {
    Pop-Location
}
