@echo off
REM Open a terminal with prma ready to go. If prma isn't installed yet, run the
REM installer first, then show the help and leave the shell open for more commands.

where prma >nul 2>nul
if errorlevel 1 (
    echo prma not found. Running installer...
    call "%~dp0Windows-Install.bat"
    REM pipx ensurepath updates PATH for *new* shells, so prma may still be
    REM missing in this one. Re-check before continuing.
    where prma >nul 2>nul
    if errorlevel 1 (
        echo.
        echo Install finished, but prma isn't on PATH in this window yet.
        echo Close and reopen this terminal, then run PRMA.bat again.
        pause
        exit /b 0
    )
)

cmd /k prma --help
