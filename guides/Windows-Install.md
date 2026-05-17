# Windows Install

1. Open PowerShell as Admin
2. `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope LocalMachine`
3. Press “a” for  { [A] Yes to All }
4. CLOSE Admin PowerShell !!!!
5. Open new non-admin terminal
6. `Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/pyenv-win/pyenv-win/master/pyenv-win/install-pyenv-win.ps1" -OutFile "./install-pyenv-win.ps1"; &"./install-pyenv-win.ps1"`
7. Close and reopen new non-admin terminal
8. `pyenv install 3.10.11`
9. `pyenv global 3.10.11`
10. `winget install --id Git.Git -e --source winget`
11. `git clone https://github.com/0187773933/PaperReferenceManagerAddons`
12. `cd PaperReferenceManagerAddons`
13. `python3 -m venv venv`
14. `.\venv\Scripts\Activate.ps1`
15. `pip install -r requirements.txt`

---

## Admin PowerShell

```
$code='Set-ExecutionPolicy RemoteSigned -Scope LocalMachine -Force; $v="3.10.11"; $u="https://raw.githubusercontent.com/pyenv-win/pyenv-win/master/pyenv-win/install-pyenv-win.ps1"; $p="$env:TEMP\install-pyenv-win.ps1"; iwr -UseBasicParsing $u -OutFile $p; & $p; $r="$env:USERPROFILE\.pyenv\pyenv-win"; $env:PYENV=$r; $env:PYENV_ROOT=$r; $env:PYENV_HOME=$r; $env:Path="$r\bin;$r\shims;$([Environment]::GetEnvironmentVariable(''Path'',''User''));$([Environment]::GetEnvironmentVariable(''Path'',''Machine''))"; if(-not((& "$r\bin\pyenv.bat" versions --bare) -contains $v)){& "$r\bin\pyenv.bat" install $v}; & "$r\bin\pyenv.bat" global $v; & "$r\bin\pyenv.bat" rehash; "@echo off`r`n""%PYENV_ROOT%\bin\pyenv.bat"" exec python %*" | Set-Content -Encoding ASCII "$r\shims\python3.bat"; python3 --version; pause'; $f="$env:TEMP\setup-pyenv-python310.ps1"; $code | Set-Content -Encoding UTF8 $f; Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$f`""
```

## Non-Admin PowerShell
```
Set-ExecutionPolicy RemoteSigned -Scope LocalMachine -Force; $v='3.10.11'; $u='https://raw.githubusercontent.com/pyenv-win/pyenv-win/master/pyenv-win/install-pyenv-win.ps1'; $p="$env:TEMP\install-pyenv-win.ps1"; iwr -UseBasicParsing $u -OutFile $p; & $p; $r="$env:USERPROFILE\.pyenv\pyenv-win"; $env:PYENV=$r; $env:PYENV_ROOT=$r; $env:PYENV_HOME=$r; $env:Path="$r\bin;$r\shims;$([Environment]::GetEnvironmentVariable('Path','User'));$([Environment]::GetEnvironmentVariable('Path','Machine'))"; if(-not((& "$r\bin\pyenv.bat" versions --bare) -contains $v)){& "$r\bin\pyenv.bat" install $v}; & "$r\bin\pyenv.bat" global $v; & "$r\bin\pyenv.bat" rehash; '@echo off`r`n"%PYENV_ROOT%\bin\pyenv.bat" exec python %*' | Set-Content -Encoding ASCII "$r\shims\python3.bat"; python3 --version​
```