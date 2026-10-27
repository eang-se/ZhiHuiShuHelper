@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0Launch-Gui.ps1"
