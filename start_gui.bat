@echo off
REM Use the hidden launcher directly. Do not use py.exe, which creates a console process.
wscript.exe "%~dp0start_gui_hidden.vbs"
exit /b 0
