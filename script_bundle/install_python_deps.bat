@echo off
setlocal

set "ROOT=%~dp0"
set "LOG_DIR=%ROOT%logs"
set "STDOUT_LOG=%LOG_DIR%\install_deps_stdout.log"
set "STDERR_LOG=%LOG_DIR%\install_deps_stderr.log"
set "PYTHON_EXE="

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

rem Try common Python installation paths first (portable across users)
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%ProgramFiles%\Python\Python314\python.exe"
    "%ProgramFiles%\Python\Python313\python.exe"
    "%ProgramFiles%\Python\Python312\python.exe"
    "%ProgramFiles%\Python\Python311\python.exe"
) do (
    if exist %%P (
        set "PYTHON_EXE=%%~P"
        goto :found_python
    )
)
where py >nul 2>nul && set "PYTHON_EXE=py" && goto :found_python
where python >nul 2>nul && set "PYTHON_EXE=python" && goto :found_python
:found_python
if not defined PYTHON_EXE (
    echo Python launcher not found.>>"%STDERR_LOG%"
    echo Python launcher not found.
    pause
    exit /b 1
)

echo ==== %date% %time% ====>>"%STDOUT_LOG%"
echo PYTHON=%PYTHON_EXE%>>"%STDOUT_LOG%"

echo Installing runtime dependencies...
"%PYTHON_EXE%" -m pip install --upgrade pip 1>>"%STDOUT_LOG%" 2>>"%STDERR_LOG%"
if errorlevel 1 goto :fail

"%PYTHON_EXE%" -m pip install fastapi uvicorn[standard] websockets pydantic pillow qrcode pywin32 requests cryptography 1>>"%STDOUT_LOG%" 2>>"%STDERR_LOG%"
if errorlevel 1 goto :fail

echo.
echo Dependencies installed successfully.
pause
exit /b 0

:fail
echo.
echo Dependency installation failed. Check:
echo %STDOUT_LOG%
echo %STDERR_LOG%
pause
exit /b 1
