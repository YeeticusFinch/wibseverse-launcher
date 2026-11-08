#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

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
    launcher.py

echo "Built app: dist/WibsverseLauncher.app"
