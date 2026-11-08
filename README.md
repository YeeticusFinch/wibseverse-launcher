# Wibsverse Launcher v0.2 — Prism wrapper

A Python/PySide6 Windows-first wrapper for **Wib's Demon Slayer**, Minecraft **1.20.1**, Forge **47.4.23**. It uses **Prism Launcher** to authenticate and launch Minecraft, and updates the Wibsverse modpack from the official Wibsverse download page.

**Status:** Source-code prototype. Nine local core tests pass. A real Prism/Windows login and graphical UI integration test **has not yet been completed**. Requires installed Prism Launcher and Java 17; does not bundle Mojang assets, the modpack, or Prism itself.

## Getting started

1. Install the regular Windows [Prism Launcher](https://prismlauncher.org/download/) and ensure you have a working Minecraft-owning account. Install/configure Java 17 through Prism.
2. Unzip this project and run it in PowerShell:
   ```powershell
   py -m pip install -r requirements.txt
   py launcher.py
   ```
3. When upgrading from the original Wibsverse prototype, approve the **one-time migration** prompt. It copies your Wibsverse instance and worlds from `%LOCALAPPDATA%\WibsverseLauncher\PrismData` into `%APPDATA%\PrismLauncher\instances\wibsverse-demon-slayer`. It keeps the old folder unchanged as a backup and will not overwrite an existing destination instance. If you decline or a migration fails, the existing isolated installation remains in use.
4. The launcher checks Wibsverse for updates on startup. If a newer version is available (or no version is installed), it opens **Updating Modpack** with download/install progress. It does not show that dialog when you're already up to date.
5. In the top-right account menu, pick your existing Microsoft Minecraft account or click **Add Microsoft account...**. The launcher automatically refreshes its menu after Prism records a successful login.
6. Click **PLAY WIBSVERSE**.

To build a Windows EXE, run `build_windows.bat` on Windows. Output is `dist\WibsverseLauncher.exe`. It still requires Prism and Java separately.

## Why a Microsoft browser login previously reopened the wrong Prism

The original Wibsverse version used its *own* Prism data root. Prism's Microsoft OAuth callback (`prismlauncher://oauth/microsoft`) is registered on Windows to the **installed Prism executable**, which usually starts against its standard root (`%APPDATA%\PrismLauncher`). As a result, the successful login could end up in the normal Prism application and be absent from the Wibsverse instance.

The revised wrapper uses **the regular Prism data root by default**. It still launches only its dedicated modpack instance, but shares Prism's account database and assets. The normal Prism window might appear briefly during login: **a stock Prism executable does not expose a supported account-login callback or account-management GUI API that can be embedded into the wrapper**. Do not register Wibsverse as the handler for Prism's OAuth scheme: Prism owns the pending PKCE login state and needs to process the callback itself.

**Preferred no-browser-redirect path:** In Prism's **Add Microsoft Account** window, use the **one-time code / QR login** by visiting https://www.microsoft.com/link and entering the code. Prism polls Microsoft and completes the login; the browser won't need to open the Prism app via a protocol prompt. Leave the Prism sign-in dialog open until it completes. The Wibsverse UI reads updated public profile metadata automatically.

**Important:** The user sees a Wibsverse account-selection menu, but Prism's account-management window is still necessary for adding or removing actual Microsoft credentials. For a *fully native* Wibsverse-owned authentication flow, a properly registered Microsoft OAuth application and supported Minecraft/Xbox authentication implementation, or a custom Prism build, would be needed. This project intentionally does not intercept OAuth codes, handle passwords, or copy account tokens into its own settings.

## New interface

- Top-right **account menu** with player name, current Minecraft skin's head (including its overlay/hat), and a grayscale placeholder when not logged in or when a skin is unavailable.
- Microsoft account switching; accounts detected from Prism's standard `accounts.json`. Only the account's public display name, type, and UUID are read. No token values are saved or displayed.
- **Add Microsoft account...**, **Add offline username...**, **Manage accounts in Prism...**, **Refresh accounts** in the dropdown.
- Offline usernames are saved as local wrapper choices and launched through Prism's `--offline` option. They are *not* inserted into Prism's account file. To create a persistent offline profile inside Prism itself, use **Manage accounts in Prism**. Offline play can require a valid Minecraft account to have been authenticated previously and cannot join ordinary online-authenticated servers.
- Top-right **settings (gear) dropdown**: Check for Updates, Set Prism Path, Game Files, Open Prism Launcher, Prism Data Folder, Switch Prism Data Folder.
- On launch, automatically check for modpack updates; show an **Updating Modpack** modal with status and a progress bar **when an update/install is needed**.
- Minecraft account player-head thumbnails use Mojang's public session profile and skin texture servers, fetched on a worker thread and cached for a day under `%LOCALAPPDATA%\WibsverseLauncher\avatars` (no auth tokens are sent).

## Files and data layout

```
%LOCALAPPDATA%\WibsverseLauncher\
  settings.json                  # local UI preferences, Prism exe/data path, offline names
  avatars\                       # public skin PNG cache
  backups\                       # updater backups for overwritten managed files
  PrismData\                     # legacy isolated root — left intact on migration

%APPDATA%\PrismLauncher\           # regular Prism root used by default
  accounts.json                  # owned and updated only by Prism (contains tokens)
  instances\
    wibsverse-demon-slayer\
      mmc-pack.json              # Minecraft 1.20.1 / Forge 47.4.23
      instance.cfg
      wibsverse-installed.json   # version, URL, hashes of managed files
      .minecraft\
        mods\, config\, saves\, ...
```

## Safety and remaining limitations

- Update discovery expects a versioned `WibsDemonSlayer-X.Y.Z.zip` link on https://wibsverse.com/download and only downloads from `https://cdn.wibsverse.com`. If that site changes, release discovery must be updated. The live ZIP format is not tested here; the updater recognizes manual ZIPs with a `mods/` subtree and common config directories.
- Archives are staged, size-checked, and checked for unsafe paths. Previously installed modified files and user world saves are preserved; replaced files are backed up. Updates should not run while another Prism session is playing Wibsverse.
- Official publisher-provided checksums or signed manifests are recommended before public distribution. The current updater calculates downloaded archive hashes but has no independent signed manifest check.
- Prism may still open an instance progress window or show errors on first launch; it is not a fully headless service.
- A private Prism install, portable executable, custom instance-directory settings, or a nonstandard Prism data root may require **Settings → Switch Prism Data Folder**. Make sure it points to the root containing the `accounts.json` that Prism's callback actually updates.
- No Windows GUI or live Microsoft login integration was available in this Linux test environment; please test on your Windows PC before distributing.
- Respect Prism GPL obligations and modpack/Minecraft redistribution terms when shipping a bundled product.

## Tests

```powershell
py -m unittest discover -s tests -v
```

Official references: [Prism command line](https://prismlauncher.org/wiki/getting-started/command-line-interface/) · [Prism data folder](https://prismlauncher.org/wiki/getting-started/data-location/) · [Prism source code](https://github.com/PrismLauncher/PrismLauncher)
