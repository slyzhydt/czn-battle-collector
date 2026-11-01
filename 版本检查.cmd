@echo off
chcp 65001 >nul
"%~dp0runtime\python.exe" -B -X utf8 "%~dp0version_report.py" %*
pause
