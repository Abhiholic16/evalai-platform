$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Virtual environment is missing: $python" }
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env'))) { throw "Missing backend/.env" }

# Both processes use the same working directory, interpreter, database, and .env.
$server = Start-Process -FilePath $python -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000','--env-file','.env' -WorkingDirectory $PSScriptRoot -PassThru
$worker = Start-Process -FilePath $python -ArgumentList '-m','app.worker' -WorkingDirectory $PSScriptRoot -PassThru
Write-Host "EvalAI server PID $($server.Id) listening on http://127.0.0.1:8000"
Write-Host "EvalAI worker PID $($worker.Id)"
Write-Host 'Leave this window open. Stop both with: Stop-Process -Id <server PID>,<worker PID>'
