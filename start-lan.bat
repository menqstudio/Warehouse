@echo off
title MenQ (LAN) - network mode
cd /d "%~dp0"
echo.
echo   MenQ - Store management  --  NETWORK / LAN mode
echo   Other devices on the same Wi-Fi/LAN can open the address shown below.
echo.
python app.py lan
pause
