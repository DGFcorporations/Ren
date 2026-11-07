@echo off
REM One-click Wren Assistant launcher for Windows
cd /d "%~dp0"

echo === Launching Wren Everyday Assistant for Windows ===

IF EXIST ".venv\Scripts\python.exe" (
    SET "PYTHON_EXEC=.venv\Scripts\python.exe"
) ELSE (
    SET "PYTHON_EXEC=python.exe"
)

echo Starting Wren native floating assistant window...
"%PYTHON_EXEC%" -m wren.cli --native %*
if %ERRORLEVEL% NEQ 0 (
    echo An error occurred running Wren.
    pause
)
