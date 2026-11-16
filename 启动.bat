@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
if exist "D:\Python\pythonw.exe" set "PY=D:\Python\pythonw.exe"
if not defined PY for %%i in (pythonw.exe) do if exist "%%~$PATH:i" set "PY=%%~$PATH:i"
if not defined PY for %%i in (python.exe) do if exist "%%~$PATH:i" set "PY=%%~$PATH:i"
if not defined PY goto err
start "" "%PY%" "%~dp0trainer.py"
exit /b 0
:err
echo 没有找到 Python 运行环境。
echo 请先安装 Python 3（python.org 下载，安装时勾选 Add Python to PATH），
echo 装好后把这个文件夹里的 启动.bat 再双击一次。
pause
