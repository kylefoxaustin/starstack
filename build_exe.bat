@echo off
rem Builds a single starstack.exe with the owl icon. Needs Python; installs PyInstaller if missing.
cd /d "%~dp0"
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean starstack.spec
echo.
echo Done: dist\starstack.exe  (double-click it, or drag a folder onto it)
pause
