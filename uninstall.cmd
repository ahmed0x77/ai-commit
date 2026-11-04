@echo off
call "%~dp0setup.cmd" uninstall
exit /b %errorlevel%
