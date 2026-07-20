@echo off
cd /d "%~dp0"
setlocal
title Unified Local LLM Agent - Mika
color 0A

:: 1. Check Python
python --version >nul 2>&1
if %errorlevel% equ 0 goto :PYTHON_OK

echo [!] Python is not installed or not in PATH!
echo [*] Downloading Python 3.11 installer...
curl -L -o python_installer.exe https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe

echo [*] Installing Python (this will take a minute)...
start /wait python_installer.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_doc=0
del python_installer.exe

echo [V] Python installation finished!
echo [!] IMPORTANT: Close this window and run Start.bat again to apply PATH changes.
pause
exit

:PYTHON_OK
echo [V] Python is installed. Checking dependencies...
python -m pip install -q --upgrade pip
python -m pip install -q textual rich pyperclip psutil win10toast Pillow

:AUTODETECT
echo.
echo [*] Searching for running local LLM servers...
set "BACKEND_ARG="

netstat -ano | find "LISTENING" | find ":5001" >nul
if %errorlevel% equ 0 (
    echo [OK] Found KoboldCpp on port 5001!
    set "BACKEND_ARG=--backend koboldcpp"
    goto :MENU
)

netstat -ano | find "LISTENING" | find ":11434" >nul
if %errorlevel% equ 0 (
    echo [OK] Found Ollama on port 11434!
    set "BACKEND_ARG=--backend ollama"
    goto :MENU
)

netstat -ano | find "LISTENING" | find ":1234" >nul
if %errorlevel% equ 0 (
    echo [OK] Found LM Studio on port 1234!
    set "BACKEND_ARG=--backend lm-studio"
    goto :MENU
)

netstat -ano | find "LISTENING" | find ":8080" >nul
if %errorlevel% equ 0 (
    echo [OK] Found llama.cpp on port 8080!
    set "BACKEND_ARG=--backend llamacpp"
    goto :MENU
)

echo [WARN] No known LLM server found! Agent will use profile defaults.

:MENU
echo.
echo ========================================================
echo               Unified Agent Start Menu (v1.6)
echo ========================================================
echo.
echo    1. Start TUI mode (Textual)
echo    2. Start Console mode (REPL)
echo    3. Exit
echo.
echo ========================================================
choice /c 123 /n /m "Choose an option (1-3): "

if errorlevel 3 goto EXIT
if errorlevel 2 goto RUN_CONSOLE
if errorlevel 1 goto RUN_TUI

:: SPDX-License-Identifier: MIT
:: Copyright (c) 2026 ByteGhost. See LICENSE for details.

:RUN_TUI
cls
echo Starting TUI mode...
python ai_agent.py %BACKEND_ARG%
echo.
pause
goto MENU

:RUN_CONSOLE
cls
echo Starting Console REPL mode...
python ai_agent.py --console %BACKEND_ARG%
echo.
pause
goto MENU

:EXIT
exit
