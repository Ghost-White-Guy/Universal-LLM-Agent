@echo off
setlocal
title Local Agent Launcher
chcp 1251 >nul

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
echo   1. Автоопределение (Сам найдет Kobold/Ollama/LM)
echo   2. Оригинальный API
echo   3. KoboldCpp (Принудительно --backend koboldcpp)
echo   4. Выход.
echo ===================================================
echo.
choice /c 1234 /n /m "Нажми 1, 2, 3 или 4: "

if errorlevel 4 goto EXIT
if errorlevel 3 goto KOBOLD
if errorlevel 2 goto ORIG
if errorlevel 1 goto AUTODETECT

:AUTODETECT
echo.
echo [Поиск] Ищем запущенные локальные нейронки...

netstat -ano | find "LISTENING" | find ":5001" >nul
if %errorlevel% equ 0 (
    echo [Успех] Найден KoboldCpp на порту 5001!
    set "BACKEND_ARG=--backend koboldcpp"
    goto PRE_RUN
)

netstat -ano | find "LISTENING" | find ":11434" >nul
if %errorlevel% equ 0 (
    echo [Успех] Найдена Ollama на порту 11434!
    set "BACKEND_ARG=--backend ollama"
    goto PRE_RUN
)

netstat -ano | find "LISTENING" | find ":1234" >nul
if %errorlevel% equ 0 (
    echo [Успех] Найден LM Studio на порту 1234!
    set "BACKEND_ARG=--backend lm-studio"
    goto PRE_RUN
)

netstat -ano | find "LISTENING" | find ":8080" >nul
if %errorlevel% equ 0 (
    echo [Успех] Найден llama.cpp на порту 8080!
    set "BACKEND_ARG=--backend llamacpp"
    goto PRE_RUN
)

echo [Ошибка] Ни одна из известных нейронок не запущена!
echo Сначала запусти сервер (Kobold, Ollama и т.д.), а потом пробуй снова.
pause
goto MENU

:ORIG
set "BACKEND_ARG="
goto PRE_RUN

:KOBOLD
set "BACKEND_ARG=--backend koboldcpp"
goto PRE_RUN

:PRE_RUN
set "SCRIPT_NAME=local_agent.py"
goto RUN

:RUN
echo.
echo Запускаем %SCRIPT_NAME%...
python "%~dp0%SCRIPT_NAME%" %BACKEND_ARG%
echo.
pause
goto MENU

:EXIT
exit