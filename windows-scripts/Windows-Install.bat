@echo off
setlocal
REM Install prma as an isolated CLI via pipx, on a pyenv-managed Python.
REM Bootstraps pyenv-win (elevated) if missing; installs the .python-version pin.
REM Run from the project root.

set EDITABLE=-e

REM --- version from .python-version ---
if not exist ".python-version" (
    echo ERROR: no .python-version in %CD%.  Run: pyenv local ^<version^>
    exit /b 1
)
set /p PYVER=<.python-version
echo Target Python: %PYVER%

REM --- pyenv ---
where pyenv >nul 2>nul
if errorlevel 1 (
    echo pyenv not found. Launching elevated installer in a new window...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Python-Install.ps1"
    echo.
    echo pyenv-win is installing in the elevated window.
    echo When it finishes, CLOSE and REOPEN this terminal, then re-run install.bat
    exit /b 0
)

REM --- ensure pinned version installed (no -s reliance on pyenv-win) ---
pyenv versions --bare | findstr /x "%PYVER%" >nul
if errorlevel 1 pyenv install %PYVER%
pyenv rehash

REM pyenv-win layout: no bin/ dir, python.exe sits directly under versions\<v>\
set PYTHON=%USERPROFILE%\.pyenv\pyenv-win\versions\%PYVER%\python.exe

REM --- pipx ---
where pipx >nul 2>nul
if errorlevel 1 (
    echo pipx not found, installing...
    python -m pip install --user pipx
    python -m pipx ensurepath
    echo PATH updated. Close and reopen this terminal, then re-run install.bat
    exit /b 0
)

pipx uninstall prma >nul 2>nul
pipx install --python "%PYTHON%" %EDITABLE% .
if errorlevel 1 ( echo Install failed. & exit /b 1 )

pipx runpip prma install -r requirements.txt
pipx inject prma "numpy<2"

echo Done. Installed with: %PYTHON%
echo Try: prma --help
echo (If 'prma' isn't found, open a new terminal so the new PATH takes effect.)