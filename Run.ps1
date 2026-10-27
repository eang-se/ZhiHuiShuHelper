param(
    [ValidateSet('run','inspect','check','export')][string]$Command = 'run',
    [ValidateSet('chromium','chrome','msedge')][string]$Browser = 'msedge'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Set-Location -LiteralPath $PSScriptRoot
$runnerPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runnerPython)) { throw 'Run Setup.ps1 first.' }
& $runnerPython (Join-Path $PSScriptRoot 'mastery.py') $Command --browser $Browser
exit $LASTEXITCODE
