@echo off
REM Open a terminal with prma ready to go. If prma isn't installed yet, run the
REM installer first, then show the help and leave the shell open for more commands.
REM NOTE: do NOT rename this to prma.bat -- a same-named .bat in the current
REM directory would shadow the real prma.exe and make this script call itself.

where prma.exe >nul 2>nul
if %errorlevel% equ 0 goto ready

echo prma not found. Running installer...
call "%~dp0Windows-Install.bat"

REM pipx ensurepath / pyenv shims only affect *new* shells, so prma may still be
REM missing in this one. Re-check before continuing.
where prma.exe >nul 2>nul
if %errorlevel% equ 0 goto ready

echo.
echo Install finished, but prma isn't on PATH in this window yet.
echo Close and reopen this terminal, then run Start-PRMA.bat again.
pause
exit /b 0

:ready
REM prma resolves --config (and other paths) relative to the current directory,
REM so run from the project root, not from windows-scripts\.
cd /d "%~dp0.."
cmd /k prma --help