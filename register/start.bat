@echo off
chcp 65001 >nul 2>&1
title postman-register
setlocal enabledelayedexpansion

REM ============================================================
REM  Windows-лаунчер реггера Postman.
REM  Сам создаёт venv, ставит зависимости, качает браузер camoufox,
REM  копирует пример конфига.
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
echo   postman-register
echo ================================
echo.

REM ---------- 1. Найти Python ----------
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
"%VENV_PY%" -c "import camoufox" >nul 2>&1
if errorlevel 1 (
    echo [*] Ставлю зависимости (camoufox, 3-5 минут)...
    "%VENV_PY%" -m pip install --upgrade pip --quiet
    if exist requirements.txt (
        "%VENV_PY%" -m pip install -r requirements.txt
    ) else (
        "%VENV_PY%" -m pip install requests camoufox
    )
    if errorlevel 1 (
        echo.
        echo [ERROR] Установка не удалась. Проверь интернет и запусти: start.bat --reinstall
        pause
        exit /b 1
    )
    echo [*] Зависимости установлены.
    echo [*] Первичная загрузка браузера camoufox (может занять пару минут)...
    "%VENV_PY%" -m camoufox fetch
)
:SKIP_DEPS

REM ---------- 4. Конфиг из примера ----------
if not exist config.json (
    if exist config.example.json (
        copy /y config.example.json config.json >nul
        echo [!] Создан config.json из примера.
        echo     ЗАПОЛНИ перед первым запуском: mail_base, mail_admin_key, mail_domain.
        echo     Прокси — по желанию, в isp_zones или brd_zones.
    )
)

REM ---------- 5. camoufox_python ----------
REM Реггер запускает camoufox отдельным процессом; на Windows это python из venv.
set CF_PY=%CD%\venv\Scripts\python.exe
echo.
echo [*] В config.json поле "camoufox_python" должно указывать на:
echo     %CF_PY%
echo     (пустое значение тоже работает — возьмётся текущий Python)
echo.

REM ---------- 6. Запуск панели ----------
set PANEL_PORT=9924
for /f "delims=" %%p in ('"%VENV_PY%" -c "import json;print(json.load(open('config.json')).get('panel_port',9924))" 2^>nul') do set PANEL_PORT=%%p
echo [*] Панель: http://127.0.0.1:%PANEL_PORT%
echo [*] Запускаю...
echo.
"%VENV_PY%" -u panel\server.py --port %PANEL_PORT%

echo.
echo [*] Процесс завершён.
pause
