# Windows Python Install

1. Open PowerShell as Admin
2. `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope LocalMachine`
3. Press “a” for  { [A] Yes to All }
4. CLOSE Admin PowerShell !!!!
5. Open new non-admin terminal
6. `Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/pyenv-win/pyenv-win/master/pyenv-win/install-pyenv-win.ps1" -OutFile "./install-pyenv-win.ps1"; &"./install-pyenv-win.ps1"`
7. Close and reopen new non-admin terminal
8. `pyenv install 3.10.11`
9. `pyenv global 3.10.11`
10. `python3 -m venv venv`
11. `.\venv\Scripts\Activate.ps1`