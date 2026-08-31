$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectDir '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw '请先安装 Python 环境及 requirements-organizer.txt 中的依赖。'
}
Start-Process -FilePath $pythonPath -ArgumentList '-m ebook_organizer' -WorkingDirectory $projectDir -WindowStyle Hidden
