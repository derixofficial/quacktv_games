@echo off
REM Restart loop for QuackTVGames bot
REM Place this file inside the TelegramBot folder.

SET SCRIPT_DIR=%~dp0

:start
echo [%DATE% %TIME%] Starting bot...
"%SCRIPT_DIR%venv\Scripts\python.exe" "%SCRIPT_DIR%bot.py"
echo [%DATE% %TIME%] Bot exited. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto start
