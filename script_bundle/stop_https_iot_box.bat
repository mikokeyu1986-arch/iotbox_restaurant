@echo off
setlocal

set "PORT=8398"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do taskkill /PID %%P /F >nul 2>nul
exit /b 0
