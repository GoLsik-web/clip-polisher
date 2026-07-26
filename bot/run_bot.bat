@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo ==========================================
echo   Бот меток Twitch — «Полировщик клипов»
echo   Канал и бот берутся из bot\config.json
echo   Останов: закрой это окно или нажми Ctrl+C
echo ==========================================
.venv\Scripts\python.exe -u -m bot.twitch_marks_bot
pause
