$ErrorActionPreference = 'Stop'
$guiPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$guiScript = Join-Path $PSScriptRoot 'desktop.py'
if (-not (Test-Path -LiteralPath $guiPython)) {
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show('Run Install.cmd first.', 'Setup required')
    exit 1
}
Start-Process -FilePath $guiPython -ArgumentList ('"{0}"' -f $guiScript) -WorkingDirectory $PSScriptRoot -WindowStyle Normal
