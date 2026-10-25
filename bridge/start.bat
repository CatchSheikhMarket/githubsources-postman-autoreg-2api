@echo off
chcp 65001 >nul 2>&1
title postman2api
setlocal enabledelayedexpansion

REM ============================================================
REM  Windows-лаунчер моста postman2api (Postman AI -> OpenAI/Anthropic).
REM  Мост написан на Go: скрипт сам собирает postman2api.exe,
REM  копирует пример конфига и поднимает сервер.
REM  Флаги: --rebuild      (пересобрать exe)
REM         --skip-build   (не собирать, запустить готовый exe)
REM ============================================================

cd /d "%~dp0"

set REBUILD=0
set SKIP_BUILD=0
for %%a in (%*) do (
    if /I "%%a"=="--rebuild" set REBUILD=1
    if /I "%%a"=="--skip-build" set SKIP_BUILD=1
)

echo ================================
echo   postman2api (мост)
echo ================================
echo.

REM ---------- 1. Конфиг ----------
if not exist config.json (
    if exist config.example.json (
        copy /y config.example.json config.json >nul
        echo [!] Создан config.json из примера.
        echo     Проверь порт (по умолчанию 12049) и default_model.
    )
)

if not exist sessions.txt (
    if exist sessions.txt.example (
        copy /y sessions.txt.example sessions.txt >nul
    ) else (
        echo # по одной сессии на строку: cookie:^<postman.sid^> > sessions.txt
    )
    echo [!] sessions.txt создан пустым.
    echo     Добавь сессию: либо запусти реггер, либо возьми postman.sid вручную
    echo     (DevTools - Application - Cookies - postman.co - postman.sid).
)

if not exist data mkdir data

REM ---------- 2. Сборка ----------
if "%SKIP_BUILD%"=="1" goto SKIP_GO
if "%REBUILD%"=="1" (
    if exist postman2api.exe (
        echo [*] --rebuild: удаляю старый exe...
        del /q postman2api.exe
    )
)

if exist postman2api.exe goto SKIP_GO

where go >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Go не найден, а готового postman2api.exe нет.
    echo.
    echo Установи Go 1.24+ : https://go.dev/dl/
    echo ВАЖНО: при установке Go сам добавит себя в PATH (перезапусти консоль).
    echo После установки запусти start.bat снова — он соберёт exe.
    echo.
    echo Или скачай готовый postman2api.exe из Releases репозитория
    echo и положи рядом с этим файлом.
    pause
    exit /b 1
)

echo [*] Go:
go version
echo [*] Собираю postman2api.exe...
set CGO_ENABLED=0
go build -o postman2api.exe .\cmd\server
if errorlevel 1 (
    echo.
    echo [ERROR] Сборка упала. Проверь, что установлен Go 1.24+.
    pause
    exit /b 1
)
echo [*] Собрано: postman2api.exe
:SKIP_GO

REM ---------- 3. Порт из конфига ----------
set PORT=12049
for /f "delims=" %%p in ('python -c "import json;print(json.load(open('config.json')).get('port',12049))" 2^>nul') do set PORT=%%p

echo.
echo [*] Мост: http://127.0.0.1:%PORT%
echo [*] Ключ для клиентов: postman2api
echo [*] Запускаю...
echo.
postman2api.exe

echo.
echo [*] Процесс завершён.
pause
