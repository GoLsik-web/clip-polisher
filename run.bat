@echo off
REM Запуск GUI «Полировщик клипов» с правильным Python из venv.
REM Двойной клик по этому файлу запускает приложение.
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

if not exist ".venv\Scripts\python.exe" (
    echo [ОШИБКА] Не найден .venv\Scripts\python.exe
    echo Сначала создайте окружение: python -m venv .venv  и  pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo Запуск приложения...
".venv\Scripts\python.exe" app.py
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
    echo.
    echo [Приложение завершилось с кодом %CODE%]
    echo Если выше есть текст ошибки — пришлите его ассистенту.
    echo.
    pause
)
endlocal
