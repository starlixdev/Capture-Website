$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Missing .venv. Follow the development setup in README.md first.'
}

& $python -m PyInstaller --noconfirm --clean (Join-Path $projectRoot 'sitecapture.spec')
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$output = Join-Path $projectRoot 'dist\CaptureWebsite.exe'
if (-not (Test-Path -LiteralPath $output)) {
    throw "Build completed without the expected executable: $output"
}

$legacyOutput = Join-Path $projectRoot 'dist\SiteCapture.exe'
if (Test-Path -LiteralPath $legacyOutput) {
    Remove-Item -LiteralPath $legacyOutput -Force
}

Write-Host "Built $output"

