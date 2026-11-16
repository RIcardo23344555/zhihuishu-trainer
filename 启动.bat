@echo off
cd /d "%~dp0"
set "PY="
if exist "D:\Python\pythonw.exe" set "PY=D:\Python\pythonw.exe"
if not defined PY for %%i in (pythonw.exe) do if exist "%%~$PATH:i" set "PY=%%~$PATH:i"
if not defined PY for %%i in (python.exe) do if exist "%%~$PATH:i" set "PY=%%~$PATH:i"
if not defined PY goto err
start "zhihuishu-trainer" "%PY%" "%~dp0trainer.py"
exit /b 0
:err
echo [ERROR] Python 3 not found.
echo Install Python 3 from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo then double-click this .bat file again.
pause
