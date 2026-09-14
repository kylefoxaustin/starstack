@echo off
rem Builds a single starstack.exe with the owl icon. Needs Python; installs PyInstaller if missing.
cd /d "%~dp0"
pip install -r requirements.txt
rem PyInstaller from source: compiles its own bootloader so Windows Defender doesn't
rem mistake the exe for every other PyInstaller app (needs the Visual Studio Build
rem Tools C++ workload; if this step fails, plain "pip install pyinstaller" works but
rem the exe may get a false-positive from Defender until Microsoft clears it).
pip install --no-binary pyinstaller --no-cache-dir pyinstaller
pyinstaller --noconfirm --clean starstack.spec
echo.
echo Done: dist\starstack.exe  (double-click it, or drag a folder onto it)
pause
