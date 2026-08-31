$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectDir
try {
    & '.\.venv\Scripts\python.exe' -m PyInstaller --noconfirm EbookOrganizer.spec
    if ($LASTEXITCODE -ne 0) { throw '打包未完成，请查看输出。' }
    Copy-Item -LiteralPath 'docs\本地整理软件使用说明.md' -Destination 'dist\EbookOrganizer\使用说明.md'
    Copy-Item -LiteralPath 'requirements-organizer-win.lock' -Destination 'dist\EbookOrganizer\构建环境.lock'
} finally { Pop-Location }
