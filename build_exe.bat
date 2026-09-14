@echo off
rem Builds a single starstack.exe with the owl icon. Needs Python; installs PyInstaller if missing.
cd /d "%~dp0"
pip install -r requirements.txt
rem A PyInstaller exe launched by the stock bootloader gets flagged by Windows
rem Defender whenever any other PyInstaller app is caught being malware. The
rem GitHub release build compiles its own bootloader (see .github/workflows/
rem release.yml -- it needs the MSVC toolchain, which most PCs don't have).
rem This local build uses the stock one; if Defender objects, use a release
rem exe or: Windows Security > Protection history > Allow.
pip install pyinstaller
python make_version_info.py
pyinstaller --noconfirm --clean starstack.spec
echo.
echo Done: dist\starstack.exe  (double-click it, or drag a folder onto it)
pause
