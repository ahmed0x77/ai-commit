@echo off
setlocal DisableDelayedExpansion
set "AI_COMMIT_ACTION="
if /I "%~1"=="uninstall" set "AI_COMMIT_ACTION=--uninstall"
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto use_py
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 goto use_python
echo Python 3.10 or newer was not found. Install Python, then run this file again.
set "AI_COMMIT_RESULT=1"
goto finished
:use_py
py -3 "%~dp0install.py" %AI_COMMIT_ACTION%
set "AI_COMMIT_RESULT=%errorlevel%"
goto finished
:use_python
python "%~dp0install.py" %AI_COMMIT_ACTION%
set "AI_COMMIT_RESULT=%errorlevel%"
:finished
echo.
if not "%AI_COMMIT_RESULT%"=="0" echo Setup failed. Read the message above; your project files were not changed.
pause
exit /b %AI_COMMIT_RESULT%
