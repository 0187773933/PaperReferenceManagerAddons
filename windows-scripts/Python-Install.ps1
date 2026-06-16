# Bootstrap pyenv-win and install the Python version pinned in .python-version.
# Self-elevates to an admin PowerShell. Called by ../install.bat; can be run directly.
# Lives in windows-scripts/, so .python-version is one level up (project root).

$root = Split-Path $PSScriptRoot -Parent
$verFile = Join-Path $root ".python-version"
if (-not (Test-Path $verFile)) {
    Write-Error "No .python-version in project root ($root). Run: pyenv local <version>"
    exit 1
}
$v = (Get-Content $verFile -Raw).Trim()
Write-Host "Target Python: $v"

# This block runs elevated. Single-quoted here-string => no interpolation here;
# the backtick escapes resolve when the elevated shell executes the line.
$code = @'
param($v)
Set-ExecutionPolicy RemoteSigned -Scope LocalMachine -Force
$u="https://raw.githubusercontent.com/pyenv-win/pyenv-win/master/pyenv-win/install-pyenv-win.ps1"
$p="$env:TEMP\install-pyenv-win.ps1"
iwr -UseBasicParsing $u -OutFile $p
& $p
$r="$env:USERPROFILE\.pyenv\pyenv-win"
$env:PYENV=$r; $env:PYENV_ROOT=$r; $env:PYENV_HOME=$r
$env:Path="$r\bin;$r\shims;$([Environment]::GetEnvironmentVariable('Path','User'));$([Environment]::GetEnvironmentVariable('Path','Machine'))"
if(-not((& "$r\bin\pyenv.bat" versions --bare) -contains $v)){& "$r\bin\pyenv.bat" install $v}
& "$r\bin\pyenv.bat" global $v
& "$r\bin\pyenv.bat" rehash
"@echo off`r`n""%PYENV_ROOT%\bin\pyenv.bat"" exec python %*" | Set-Content -Encoding ASCII "$r\shims\python3.bat"
python3 --version
pause
'@

$f = "$env:TEMP\setup-pyenv.ps1"
$code | Set-Content -Encoding UTF8 $f
Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$f`" -v `"$v`""