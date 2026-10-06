param(
    [string]$Url = 'https://example.com',
    [switch]$Full
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Missing .venv. Follow the development setup in README.md first.'
}

$arguments = @('-m', 'sitecapture', $Url, '--desktop', '--timeout', '120', '--output', (Join-Path $projectRoot 'output'))
if ($Full) {
    $arguments += '--full'
}
& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Real capture failed with exit code $LASTEXITCODE."
}

& $python (Join-Path $projectRoot 'scripts\verify-latest-capture.py') (Join-Path $projectRoot 'output')
if ($LASTEXITCODE -ne 0) {
    throw "Capture verification failed with exit code $LASTEXITCODE."
}

