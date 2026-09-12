@echo off
REM Start IoT Box from Python source (includes /printer_iot/printer endpoint fix)
REM Python 3.11 must be installed at the path below
set PYTHON_PATH=C:\Users\miko\AppData\Local\Programs\Python\Python311\python.exe

if not exist "%PYTHON_PATH%" (
    echo ERROR: Python not found at %PYTHON_PATH%
    echo Please install Python 3.11 first.
    pause
    exit /b 1
)

cd /d "%~dp0"
echo Starting IoT Box from source (HTTPS only)...
echo Endpoint: https://127.0.0.1:8398
echo Press Ctrl+C to stop.

"%PYTHON_PATH%" run_https.py
pause
