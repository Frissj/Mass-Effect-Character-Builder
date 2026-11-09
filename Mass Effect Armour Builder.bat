
@echo off
setlocal
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Installing Python 3.13 with winget ...
    winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements
    echo Run this launcher again after Python is installed.
    pause
    exit /b
)
python "%~dp0mele_builder.py" %*
if errorlevel 1 pause
