param(
    [string]$PythonExecutable,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$ReleaseName = 'GrokDesk'
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $PythonExecutable) { $PythonExecutable = Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $pythonExecutable)) { throw '请先按使用说明创建 .venv 并安装依赖。' }
$releaseDirectory = Join-Path $PSScriptRoot ("dist\$ReleaseName")
$releaseArchive = Join-Path $PSScriptRoot ("dist\$ReleaseName-Windows.zip")
$releaseExecutable = Join-Path $releaseDirectory 'GrokDesk.exe'
if (Get-Process -Name GrokDesk -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $releaseExecutable }) {
    throw '请先在设置中点击「退出软件」再打包。'
}
$stagingRoot = Join-Path $PSScriptRoot ('build\release-staging-' + [Guid]::NewGuid().ToString('N'))
$stagingDist = Join-Path $stagingRoot 'dist'
$stagingBundle = Join-Path $stagingDist 'GrokDesk'
New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
try {
    & $pythonExecutable -c 'import webview, clr'
    if ($LASTEXITCODE -ne 0) { throw '桌面组件缺失，请先安装 requirements.txt 中的依赖。' }
    & $pythonExecutable tests\collect_licenses.py --output (Join-Path $stagingRoot 'third_party')
    if ($LASTEXITCODE -ne 0) { throw '许可证收集失败。' }
    $webResources = (Join-Path $PSScriptRoot 'web') + ';web'
    & $pythonExecutable -m PyInstaller --noconfirm --clean --onedir --windowed --name GrokDesk --distpath $stagingDist --workpath (Join-Path $stagingRoot 'work') --specpath $stagingRoot --icon (Join-Path $PSScriptRoot 'web\icon.ico') --add-data $webResources --hidden-import webview.platforms.winforms --hidden-import webview.platforms.edgechromium (Join-Path $PSScriptRoot 'app.py')
    if ($LASTEXITCODE -ne 0) { throw '打包失败。' }
    Copy-Item -LiteralPath '使用说明.md' -Destination $stagingBundle -Force
    Copy-Item -LiteralPath '浏览器备用启动.vbs' -Destination $stagingBundle -Force
    Copy-Item -LiteralPath '启动 GrokDesk.vbs' -Destination $stagingBundle -Force
    Copy-Item -LiteralPath (Join-Path $stagingRoot 'third_party') -Destination $stagingBundle -Recurse -Force
    Copy-Item -LiteralPath 'web\icon.png' -Destination $stagingBundle -Force
    Copy-Item -LiteralPath 'web\icon.ico' -Destination $stagingBundle -Force
    New-Item -ItemType Directory -Path (Split-Path -Parent $releaseDirectory) -Force | Out-Null
    Compress-Archive -Path (Join-Path $stagingBundle '*') -DestinationPath $releaseArchive -CompressionLevel Optimal -Force
    New-Item -ItemType Directory -Path $releaseDirectory -Force | Out-Null
    Copy-Item -Path (Join-Path $stagingBundle '*') -Destination $releaseDirectory -Recurse -Force
}
finally {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "已生成 $releaseArchive 和 $(Join-Path $releaseDirectory 'GrokDesk.exe')"
