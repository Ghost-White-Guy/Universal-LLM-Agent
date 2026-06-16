@echo off
title Local Agent Launcher
chcp 65001 >nul

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% equ 0 goto :PYTHON_OK

echo Python is not installed or not in PATH!
echo Downloading Python 3.11...
curl -L -o python_installer.exe https://www.python.org/ftp/python/3.11.8/python-3.11.8-amd64.exe

echo Installing Python (please wait)...
start /wait python_installer.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_doc=0
del python_installer.exe

echo Applying new PATH for current session...
set "PATH=%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts;%PATH%"

:PYTHON_OK
echo Python is installed.
echo Installing required libraries...
python -m pip install --upgrade pip -q
python -m pip install psutil win10toast Pillow -q

:MENU
cls
echo ===================================================
echo               ВЫБЕРИ ВЕРСИЮ АГЕНТА
echo ===================================================
echo   1. Оригинальный  (local_agent.py)
echo   2. Выход.
echo ===================================================
echo.
REM Добавили скрытую тройку в /c 123
choice /c 12 /n /m "Нажми 1 или 2: "

if errorlevel 2 goto EXIT
if errorlevel 1 goto ORIG

:EXIT
exit

:ORIG
set SCRIPT_NAME=local_agent.py
goto RUN

:RUN
echo.
echo Запускаем %SCRIPT_NAME%...
REM Скрипт сам спросит ключ, если его нет в профиле пользователя!
python "%~dp0%SCRIPT_NAME%"
pause