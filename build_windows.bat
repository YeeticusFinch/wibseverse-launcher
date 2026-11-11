@echo off
setlocal
cd /d "%~dp0"
gradle -p launcher-link-bridge build
if errorlevel 1 exit /b 1
if not exist ".build-venv\Scripts\python.exe" (
    py -m venv ".build-venv"
    if errorlevel 1 exit /b 1
)
".build-venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller pillow
if errorlevel 1 exit /b 1
".build-venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name WibsverseLauncher --icon "assets\wibsverse_icon.png" --add-data "assets;assets" --add-data "launcher-link-bridge\build\libs\wibsverse-launcher-link-bridge-1.0.0.jar;mods" --collect-all keyring --collect-all cryptography --collect-all psutil launcher.py
if errorlevel 1 exit /b 1
echo Built executable: dist\WibsverseLauncher.exe
