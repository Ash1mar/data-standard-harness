param(
    [ValidateSet('app','mcp','setup')][string]$Action = 'app',
    [int]$Port = 8123,
    [switch]$UseLocalOllama
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
if ($UseLocalOllama) {
    $env:LLM_BASE_URL = 'http://127.0.0.1:11434/v1'
    $env:LLM_API_KEY = 'local-ollama'
    $env:LLM_MODEL = 'gemma4:31b-cloud'
    $env:LLM_BULK_MODEL = 'gemma4:31b-cloud'
    $env:LLM_TIMEOUT = '180'
}
& $pythonPath -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
