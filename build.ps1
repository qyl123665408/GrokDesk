param([string]$PythonExecutable)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $PythonExecutable) { $PythonExecutable = Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $pythonExecutable)) { throw '请先按使用说明创建 .venv 并安装依赖。' }
$releaseExecutable = Join-Path $PSScriptRoot 'dist\GrokDesk\GrokDesk.exe'
if (Get-Process -Name GrokDesk -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $releaseExecutable }) {
    throw '请先在设置中点击「退出软件」再打包。'
}
$runtimeFolder = Join-Path $PSScriptRoot 'dist\GrokDesk\GrokDeskData'
$runtimeBackup = $null
if (Test-Path -LiteralPath $runtimeFolder) {
    $runtimeBackup = Join-Path $PSScriptRoot ('build\runtime-backup-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $runtimeBackup -Force | Out-Null
    Copy-Item -LiteralPath $runtimeFolder -Destination $runtimeBackup -Recurse -Force
}
try {
    & $pythonExecutable -c 'import webview, clr'
    if ($LASTEXITCODE -ne 0) { throw '桌面组件缺失，请先安装 requirements.txt 中的依赖。' }
    & $pythonExecutable tests\collect_licenses.py
    if ($LASTEXITCODE -ne 0) { throw '许可证收集失败。' }
    & $pythonExecutable -m PyInstaller --noconfirm --clean --onedir --windowed --name GrokDesk --icon web\icon.ico --add-data 'web;web' --hidden-import webview.platforms.winforms --hidden-import webview.platforms.edgechromium app.py
    if ($LASTEXITCODE -ne 0) { throw '打包失败。' }
    Copy-Item -LiteralPath '使用说明.md' -Destination 'dist\GrokDesk\使用说明.md' -Force
    Copy-Item -LiteralPath '浏览器备用启动.vbs' -Destination 'dist\GrokDesk\浏览器备用启动.vbs' -Force
    Copy-Item -LiteralPath '启动 GrokDesk.vbs' -Destination 'dist\GrokDesk\启动 GrokDesk.vbs' -Force
    Copy-Item -LiteralPath 'third_party' -Destination 'dist\GrokDesk\third_party' -Recurse -Force
    Copy-Item -LiteralPath 'web\icon.png' -Destination 'dist\GrokDesk\icon.png' -Force
    Copy-Item -LiteralPath 'web\icon.ico' -Destination 'dist\GrokDesk\icon.ico' -Force
    # Archive the clean release before restoring this computer's private history.
    Compress-Archive -Path 'dist\GrokDesk\*' -DestinationPath 'dist\GrokDesk-Windows.zip' -CompressionLevel Optimal -Force
}
finally {
    if ($runtimeBackup) {
        New-Item -ItemType Directory -Path 'dist\GrokDesk' -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $runtimeBackup 'GrokDeskData') -Destination 'dist\GrokDesk' -Recurse -Force
    }
}
Write-Host '已生成 dist\GrokDesk-Windows.zip 和 dist\GrokDesk\GrokDesk.exe'
