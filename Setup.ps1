param([string]$PythonPath = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    $candidates = @()
    if ($PythonPath) { $candidates += $PythonPath }
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundledPython) { $candidates += $bundledPython }
    $installedPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($installedPython -and $installedPython.Source -notlike '*\WindowsApps\*') { $candidates += $installedPython.Source }
    if (-not $candidates) { throw 'Python 3.10+ is required. Install Python or pass -PythonPath C:\path\python.exe.' }
    & $candidates[0] -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python virtual environment.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$edgeLocations = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
    (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
)
if (-not ($edgeLocations | Where-Object { Test-Path -LiteralPath $_ })) {
    & '.\.venv\Scripts\python.exe' -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Chromium installation failed.' }
    Write-Host 'Edge not found. Start with: .\Run.ps1 -Browser chromium'
}
if (-not (Test-Path -LiteralPath 'profile.json')) {
    Copy-Item -LiteralPath 'profile.polymas.json' -Destination 'profile.json'
}
Write-Host 'Ready. Double-click Start.cmd to run.'
