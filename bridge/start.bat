@echo off
chcp 65001 >nul 2>&1
title postman2api
setlocal enabledelayedexpansion

REM ============================================================
REM  Windows-лаунчер моста postman2api (Postman AI -> OpenAI/Anthropic).
REM  Чистый Python: без Go, без сборки exe.
REM  Флаги: --reinstall    (пересоздать venv)
REM         --skip-install (не ставить зависимости, просто запустить)
REM ============================================================

cd /d "%~dp0"

set REINSTALL=0
set SKIP_INSTALL=0
for %%a in (%*) do (
    if /I "%%a"=="--reinstall" set REINSTALL=1
    if /I "%%a"=="--skip-install" set SKIP_INSTALL=1
)

echo ================================
echo   postman2api (мост)
echo ================================
echo.

REM ---------- 1. Python ----------
set PYTHON_BIN=
for %%P in (python python3 py) do (
    if not defined PYTHON_BIN (
        %%P --version >nul 2>&1
        if not errorlevel 1 set PYTHON_BIN=%%P
    )
)
if not defined PYTHON_BIN (
    echo [ERROR] Python не найден.
    echo Установи Python 3.10+ : https://www.python.org/downloads/
    echo ВАЖНО: при установке отметь "Add Python to PATH".
    pause
    exit /b 1
)
echo [*] Python:
%PYTHON_BIN% --version

REM ---------- 2. venv ----------
if "%REINSTALL%"=="1" (
    if exist venv (
        echo [*] --reinstall: удаляю старый venv...
        rmdir /s /q venv
    )
)
set VENV_PY=venv\Scripts\python.exe
if not exist "%VENV_PY%" (
    echo [*] Создаю виртуальное окружение...
    %PYTHON_BIN% -m venv venv
    if errorlevel 1 (
        echo [ERROR] Не удалось создать venv. Переустанови Python с полными компонентами.
        pause
        exit /b 1
    )
)

REM ---------- 3. Зависимости ----------
if "%SKIP_INSTALL%"=="1" goto SKIP_DEPS
"%VENV_PY%" -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo [*] Ставлю зависимости...
    "%VENV_PY%" -m pip install --upgrade pip --quiet
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Установка не удалась. Проверь интернет и запусти: start.bat --reinstall
        pause
        exit /b 1
    )
)
:SKIP_DEPS

REM ---------- 4. Конфиг и сессии ----------
if not exist config.json (
    if exist config.example.json (
        copy /y config.example.json config.json >nul
        echo [!] Создан config.json из примера.
    )
)
if not exist sessions.txt (
    if exist sessions.txt.example (
        copy /y sessions.txt.example sessions.txt >nul
    ) else (
        echo # по одной сессии на строку: cookie:^<postman.sid^> > sessions.txt
    )
    echo [!] sessions.txt создан пустым. Добавь сессию или запусти реггер.
)
if not exist data mkdir data

REM ---------- 5. Порт и запуск ----------
set PORT=12049
for /f "delims=" %%p in ('"%VENV_PY%" -c "import json;print(json.load(open('config.json')).get('port',12049))" 2^>nul') do set PORT=%%p

echo.
echo [*] Мост: http://127.0.0.1:%PORT%
echo [*] Ключ для клиентов: postman2api
echo [*] Запускаю...
echo.
"%VENV_PY%" -u server.py

echo.
echo [*] Процесс завершён.
pause