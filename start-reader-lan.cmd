@echo off
setlocal
cd /d "%~dp0"

echo Article Reader LAN mode makes the app reachable on your private local network.
echo Phones still need a one-time pairing code. HTTP traffic is not encrypted.
echo.
choice /C YN /N /M "Start LAN mode now? [Y/N] "
if errorlevel 2 exit /b 0

call "%~dp0start-reader.cmd" lan
exit /b %ERRORLEVEL%
