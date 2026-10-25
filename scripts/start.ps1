param(
    [ValidateSet('app','mcp','setup')][string]$Action = 'app',
    [int]$Port = 8123
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv-local\Scripts\python.exe'
Set-Location -LiteralPath $projectRoot
if ($Action -eq 'setup') {
    if (-not (Test-Path -LiteralPath $pythonPath)) { python -m venv .venv-local }
    & $pythonPath -m pip install -r requirements.txt
    exit $LASTEXITCODE
}
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run scripts/start.ps1 setup first.' }
$env:PYTHONIOENCODING = 'utf-8'
if ($Action -eq 'mcp') { & $pythonPath backend/harness_mcp.py; exit $LASTEXITCODE }
$env:STANDARD_EXECUTION_MODE = 'live'
& $pythonPath -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
