@echo off
cd /d "%~dp0"

powershell.exe -NoExit -ExecutionPolicy Bypass -Command ^
  "& { . .\venv\Scripts\Activate.ps1; python main.py server --zotero }"