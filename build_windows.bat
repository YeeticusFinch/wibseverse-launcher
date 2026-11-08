@echo off
setlocal
py -m pip install -r requirements.txt pyinstaller
py -m PyInstaller --noconfirm --clean --onefile --windowed --name WibsverseLauncher launcher.py
if errorlevel 1 exit /b 1
echo Built executable: dist\WibsverseLauncher.exe
