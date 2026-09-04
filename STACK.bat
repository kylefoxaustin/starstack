@echo off
rem starstack -- drag a folder onto this file, or double-click it and pick one.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (start "" pythonw "button.py" %1) else (start "" python "button.py" %1)
