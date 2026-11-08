# Wibsverse Launcher

A Python/PySide6 Windows-first launcher for **Wib's Demon Slayer**, Minecraft **1.20.1**, Forge **47.4.23**. It uses **HeadlessMc** as a hidden backend for Microsoft authentication and Minecraft launching, while updating the Wibsverse modpack from the official download page.

**Status:** Source-code prototype. Automated tests cover the updater, skin fallback, HeadlessMc commands, and runtime setup. A real Microsoft login and Forge launch still need to be verified on Windows.

## Requirements

- Windows 10 or newer
- A Microsoft account that owns Minecraft Java Edition if using online authentication
- Internet access for the first HMC, Java, Forge, and modpack setup

If `headlessmc.exe` is not found, the launcher automatically downloads the pinned official Windows x64 release (`3.0.0-RC8`) from GitHub and verifies its SHA-256 checksum. It installs the binary under `HeadlessMc\bin` in the launcher data directory. If Java 17 is not available, it downloads and verifies a private Temurin 17 runtime under HMC's isolated data directory. You can also select an existing HMC executable through Settings → Set HeadlessMc Path.

## Getting Started

```powershell
py -m pip install -r requirements.txt
py launcher.py
```

1. Open the account menu or the **ADD ACCOUNT** button.
2. Choose a Microsoft account and complete the HeadlessMc device-code login, or choose an offline account and enter a player name. Offline accounts do not require Microsoft sign-in.
3. Install or wait for the Wibsverse modpack update.
4. Click **PLAY WIBSVERSE**. Java 17 and the pinned Forge profile are prepared automatically.

The launcher never opens Prism. HeadlessMc owns and refreshes Microsoft credentials; the wrapper only reads the public account table containing player name, UUID, account type, and provider. Offline account records are tokenless and are stored in HMC's isolated offline account store.

## HeadlessMc Integration

- Login uses `headlessmc account --provider default login`.
- Account selection uses `headlessmc account select NAME`.
- Offline accounts are created as tokenless HMC-compatible records and can be removed from the account menu.
- The launcher explicitly installs/verifies `forge 1.20.1 47.4.23`.
- Launch uses `headlessmc launch wibsverse` from a hidden subprocess, so Minecraft keeps its normal graphical client.
- HMC-Specifics is disabled because it does not currently support Forge 1.20.1.
- The server join option adds `--server play.wibsverse.com`.

The wrapper isolates HeadlessMc's XDG data/config/state/cache directories below:

```text
%LOCALAPPDATA%\WibsverseLauncher\
  settings.json
  avatars\
  headlessmc-launch.log
  HeadlessMc\
    bin\headlessmc.exe     # automatically installed HMC backend
    data\headlessmc\       # Java runtime and Microsoft credentials
    config\headlessmc\     # wrapper-owned HMC configuration
      profiles\            # pinned Wibsverse Forge profile
    minecraft\              # HMC-managed versions and libraries
    game\                   # Wibsverse mods, configs, saves, and options
```

The wrapper never copies or displays HMC account tokens.

## Build

On Windows:

```powershell
build_windows.bat
```

Output: `dist\WibsverseLauncher.exe`. The build includes `assets\wibsversehero.png`; HMC and Java are installed by the launcher when needed.

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
