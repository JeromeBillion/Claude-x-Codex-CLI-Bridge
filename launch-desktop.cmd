@echo off
setlocal
cd /d "%~dp0" || exit /b 1

where py >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python 3.11 or newer is required. Install it, then run this launcher again.
        pause
        exit /b 1
    )
    set "PYTHON_CMD=python"
)

%PYTHON_CMD% -c "import sys; sys.exit(sys.version_info < (3, 11))"
if errorlevel 1 (
    echo Python 3.11 or newer is required.
    pause
    exit /b 1
)

if /i "%~1"=="--smoke" (
    %PYTHON_CMD% -c "import tkinter as tk; from tools.desktop import DesktopHost; root=tk.Tk(); DesktopHost(root); root.update_idletasks(); root.destroy(); print('Desktop window constructed; no model turn')"
    exit /b %errorlevel%
)

%PYTHON_CMD% -m tools.desktop
if errorlevel 1 (
    echo Desktop host exited with an error. Check the installed Python and CLI setup.
    pause
    exit /b 1
)
