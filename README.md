# Wibsverse Launcher

<img width="2379" height="1496" alt="image" src="https://github.com/user-attachments/assets/d23d9db4-b235-413e-86e3-88c31fef4003" />

A Python/PySide6 launcher for **Wib's Demon Slayer**, Minecraft **1.20.1**, Forge **47.4.23**, supporting Windows and (maybe) macOS. It uses **HeadlessMc** as a hidden backend for Microsoft authentication and Minecraft launching, while updating the Wibsverse modpack from the official download page.

**Status:** Source-code prototype. Automated tests cover the updater, skin fallback, HeadlessMc commands, and runtime setup.

If HeadlessMc is not found, the launcher automatically downloads the pinned official release (`3.0.0-RC8`) for the current operating system and CPU architecture from GitHub, then verifies its SHA-256 checksum. It installs the binary under `HeadlessMc/bin` in the launcher data directory. If Java 17 is not available, it downloads and verifies a private Temurin 17 runtime under HMC's isolated data directory. You can also select an existing HMC executable through Settings → Set HeadlessMc Path.

## Getting Started 

The launcher can be installed from the released tab.

1. Open the account menu or the **ADD ACCOUNT** button.
2. Choose a Microsoft account and complete the HeadlessMc device-code login, or choose an offline account and enter a player name. Offline accounts do not require Microsoft sign-in.
3. Install or wait for the Wibsverse modpack update.
4. Click **PLAY WIBSVERSE**. Java 17 and the pinned Forge profile are prepared automatically.

## Getting Started (developer environment)

```powershell
py -m pip install -r requirements.txt
py launcher.py
```

## HeadlessMc Integration

- Login uses `headlessmc account --provider default login`.
- Account selection uses `headlessmc account select NAME`.
- Offline accounts are created as tokenless HMC-compatible records and can be removed from the account menu.
- The launcher explicitly installs/verifies `forge 1.20.1 47.4.23`.
- Launch uses `headlessmc launch wibsverse` from a hidden subprocess, so Minecraft keeps its normal graphical client.
- HMC-Specifics is disabled because it does not currently support Forge 1.20.1.
- The server join option adds `--server play.wibsverse.com`.

The wrapper isolates HeadlessMc's data/config/state/cache directories below the platform-specific launcher data directory:

```text
Windows: `%LOCALAPPDATA%\WibsverseLauncher\`
macOS: `~/Library/Application Support/WibsverseLauncher/`

```text
  settings.json
  avatars\
  headlessmc-launch.log
  HeadlessMc\
    bin\headlessmc         # automatically installed HMC backend; .exe on Windows
    data\headlessmc\       # Java runtime and Microsoft credentials
    config\headlessmc\     # wrapper-owned HMC configuration
      profiles\            # pinned Wibsverse Forge profile
    minecraft\              # HMC-managed versions and libraries
    game\                   # Wibsverse mods, configs, saves, and options
```

The wrapper never copies or displays HMC account tokens.

## Build

Build on the operating system you are packaging for. PyInstaller does not reliably cross-compile Windows and macOS applications.

### Windows

Requirements: Windows 10 or newer, Python 3.11 or newer, and internet access.

```powershell
build_windows.bat
```

The script creates `.build-venv`, installs the build dependencies, and produces `dist\WibsverseLauncher.exe`. HMC and Java are installed by the launcher when needed.

### macOS

Requirements: macOS, Python 3.11 or newer, and internet access. Run the build on the target architecture: an Intel Mac produces an x64 `.app`; an Apple Silicon Mac produces an arm64 `.app`.

```zsh
chmod +x build_macos.sh
./build_macos.sh
```

The script creates `.build-venv`, installs the build dependencies, and produces `dist/WibsverseLauncher.app`. Create a release-ready archive with:

```zsh
ditto -c -k --sequesterRsrc --keepParent \
  "dist/WibsverseLauncher.app" \
  "dist/WibsverseLauncher-mac-$(uname -m).zip"
```

Upload the resulting `.zip` to the GitHub release. The filename should include `mac`, such as `WibsverseLauncher-mac-x86_64.zip` or `WibsverseLauncher-mac-arm64.zip`.

### Automated Windows and macOS builds

The `.github/workflows/build-release.yml` workflow builds all three native packages:

- Windows x64 executable
- macOS Intel `.app` archive
- macOS Apple Silicon `.app` archive

Run it manually from the repository's **Actions** tab to produce build artifacts. To upload a manual run to an existing release, enter its tag in the optional `release_tag` field. Or push a numeric version tag to build and publish a GitHub release automatically:

```powershell
git tag 0.4
git push origin 0.4
```

The tag must match the `LAUNCHER_VERSION` value in `core.py`.

## Launcher Versioning and Updates

The current launcher version is defined in one place:

```python
# core.py
LAUNCHER_VERSION = "0.3"
```

When releasing a new launcher version:

1. Change `LAUNCHER_VERSION` in `core.py`.
2. Build the launcher for the supported platform(s).
3. Commit the change and push a matching numeric tag, such as `0.4`; the GitHub Actions workflow builds and publishes the release assets automatically.
4. If uploading assets manually, Windows assets should be `.exe` or `.msi`; macOS assets should be `.dmg`, `.pkg`, or `.zip` and include `mac`, `darwin`, or `osx` in the filename.
5. Publish the release. Draft releases are ignored; prereleases are checked as well.

At startup, the launcher checks the GitHub releases API. If a newer release is found, it prompts the user with **Update Launcher**. The button downloads and opens the compatible release asset, then closes the current launcher. If no compatible asset is attached, it opens the release page instead.

## Tests

```powershell
py -m unittest discover -s tests -v
```

## Safety and Limitations

- The updater only accepts versioned ZIP links from `https://cdn.wibsverse.com`.
- ZIP paths, sizes, symlinks, checksums, backups, and rollback are validated by the updater.
- Existing worlds and personal files are preserved.
- The exact Forge build must be available to HeadlessMc; the launcher refuses to launch a profile that is not `1.20.1-47.4.23`.
- First launch may download Java, Forge, libraries, assets, and the modpack.
