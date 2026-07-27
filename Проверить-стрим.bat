@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ==========================================================
echo   Проверка автопоиска моментов (Этап 3)
echo   Вставь ссылку на запись стрима или ник канала.
echo   Видео НЕ качается: берётся только звуковая дорожка.
echo.
echo   Ключи (по желанию):
echo     --no-audio       не слушать звук (быстро, только клипы и чат)
echo     --speech skip    не распознавать речь
echo     --file "путь"    взять запись с диска, ничего не качать
echo     --strict 80      строже отбор (0..100)
echo ==========================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ОШИБКА] Не найден .venv\Scripts\python.exe
    echo Сначала создайте окружение: python -m venv .venv
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m dev.scan_link %*

echo.
pause
