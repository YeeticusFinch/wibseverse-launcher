#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

gradle -p launcher-link-bridge build

if [[ ! -x ".build-venv/bin/python" ]]; then
    python3 -m venv ".build-venv"
fi

".build-venv/bin/python" -m pip install -r requirements.txt pyinstaller pillow
".build-venv/bin/python" -m PyInstaller \
    --noconfirm \
    --clean \
    --windowed \
    --name WibsverseLauncher \
    --icon "assets/wibsverse_icon.png" \
    --add-data "assets:assets" \
    --add-data "launcher-link-bridge/build/libs/wibsverse-launcher-link-bridge-1.0.0.jar:mods" \
    --collect-all keyring \
    --collect-all cryptography \
    --collect-all psutil \
    launcher.py

echo "Built app: dist/WibsverseLauncher.app"
