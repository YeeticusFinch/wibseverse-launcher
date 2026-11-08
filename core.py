"""Wibsverse Launcher: Prism instance and safe modpack updater (Python 3.10+)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Callable

DOWNLOAD_PAGE = "https://wibsverse.com/download"
SERVER = "play.wibsverse.com"
MINECRAFT_VERSION = "1.20.1"
FORGE_VERSION = "47.4.23"
INSTANCE_ID = "wibsverse-demon-slayer"
MAX_ARCHIVE_BYTES = 4 * 1024**3
MAX_UNPACKED_BYTES = 8 * 1024**3
ALLOWED_DIRS = {
    "mods", "config", "defaultconfigs", "kubejs", "scripts", "resourcepacks",
    "shaderpacks", "patchouli_books", "texturepacks", "journeymap", "xaero",
    "openloader", "customnpcs", "schematics", "global_packs", "fancymenu",
}
ZIP_VERSION_RE = re.compile(r"Wibs?DemonSlayer[-_](\d+(?:\.\d+){1,3})\.zip$", re.I)
Progress = Callable[[str, int], None]


class LauncherError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    sha256: str | None = None

    def validate(self) -> None:
        u = urllib.parse.urlparse(self.url)
        if u.scheme != "https" or u.hostname != "cdn.wibsverse.com":
            raise LauncherError("The pack URL must be HTTPS on cdn.wibsverse.com")
        if not u.path.lower().endswith(".zip"):
            raise LauncherError("Expected a .zip modpack archive")
        if not re.fullmatch(r"\d+(?:\.\d+){1,3}", self.version):
            raise LauncherError("Invalid modpack version")
        if self.sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256):
            raise LauncherError("Invalid SHA-256 checksum")


class DownloadLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.urls.append(urllib.parse.urljoin(DOWNLOAD_PAGE, href))


def parse_official_download_page(html: str) -> Release:
    p = DownloadLinks()
    p.feed(html)
    possible = []
    for url in p.urls:
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname != "cdn.wibsverse.com" or "/modpacks/ds/manual/" not in parsed.path:
            continue
        match = ZIP_VERSION_RE.search(PurePosixPath(parsed.path).name)
        if match:
            possible.append(Release(match.group(1), url))
    if not possible:
        raise LauncherError("Could not find a versioned manual ZIP on wibsverse.com/download")
    # If multiple are present, prefer the numerically newest version.
    release = max(possible, key=lambda r: tuple(int(x) for x in r.version.split(".")))
    release.validate()
    return release


def fetch_latest_release(timeout: int = 18) -> Release:
    request = urllib.request.Request(
        DOWNLOAD_PAGE, headers={"User-Agent": "WibsverseLauncher/0.1 (+modpack-update-check)", "Accept": "text/html"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise LauncherError("Wibsverse download page is unexpectedly large")
        html = body.decode("utf-8", errors="replace")
    return parse_official_download_page(html)


def app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "WibsverseLauncher"


def prism_data_dir() -> Path:
    return app_dir() / "PrismData"


def instance_dir(root: Path) -> Path:
    return root / "instances" / INSTANCE_ID


def game_dir(root: Path) -> Path:
    return instance_dir(root) / ".minecraft"


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_installed(root: Path) -> dict:
    data = load_json(instance_dir(root) / "wibsverse-installed.json", {})
    return data if isinstance(data, dict) else {}


def ensure_instance(root: Path) -> None:
    folder = instance_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".minecraft").mkdir(exist_ok=True)
    cfg = folder / "instance.cfg"
    pack = folder / "mmc-pack.json"
    if not cfg.exists():
        cfg.write_text(
            "InstanceType=OneSix\nname=Wib's Demon Slayer\n"
            "OverrideMemory=true\nMinMemAlloc=2048\nMaxMemAlloc=6144\n",
            encoding="utf-8",
        )
    if not pack.exists():
        atomic_json(pack, {
            "formatVersion": 1,
            "components": [
                {"uid": "net.minecraft", "version": MINECRAFT_VERSION, "important": True},
                {"uid": "net.minecraftforge", "version": FORGE_VERSION},
            ],
        })
    else:
        # Don't overwrite user/customized Prism instance version settings.
        current = load_json(pack, {})
        components = current.get("components", []) if isinstance(current, dict) else []
        versions = {c.get("uid"): c.get("version") for c in components if isinstance(c, dict)}
        if versions.get("net.minecraft") != MINECRAFT_VERSION or versions.get("net.minecraftforge") != FORGE_VERSION:
            raise LauncherError(
                f"Instance's Minecraft/Forge versions differ from {MINECRAFT_VERSION}/{FORGE_VERSION}. "
                "Correct them in Prism before continuing."
            )


def _safe_normalize(filename: str) -> str | None:
    """Map manual-pack paths to a managed game-relative path; reject unsafe entries."""
    filename = filename.replace("\\", "/")
    if filename.startswith("/") or re.match(r"^[A-Za-z]:", filename):
        raise LauncherError(f"Unsafe absolute ZIP path: {filename}")
    parts = [p for p in filename.split("/") if p not in ("", ".")]
    if ".." in parts or any("\x00" in p or ":" in p or p.endswith(".") for p in parts):
        raise LauncherError(f"Unsafe ZIP path: {filename}")
    if not parts:
        return None
    for i, p in enumerate(parts):
        if p.lower() in ALLOWED_DIRS and i < len(parts) - 1:
            return "/".join([p.lower(), *parts[i + 1:]])
    if len(parts) <= 2 and parts[-1].lower().endswith(".jar"):
        return "mods/" + parts[-1]
    return None


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def _download_archive(release: Release, destination: Path, progress: Progress) -> str:
    release.validate()
    req = urllib.request.Request(release.url, headers={"User-Agent": "WibsverseLauncher/0.1"})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(req, timeout=45) as response, destination.open("wb") as dst:
        final = urllib.parse.urlparse(response.geturl())
        if final.scheme != "https" or final.hostname != "cdn.wibsverse.com":
            raise LauncherError("Unexpected redirect while downloading modpack")
        expected = int(response.headers.get("Content-Length", 0))
        if expected > MAX_ARCHIVE_BYTES:
            raise LauncherError("Archive exceeds download size limit")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise LauncherError("Archive exceeds download size limit")
            digest.update(chunk)
            dst.write(chunk)
            if expected:
                progress(f"Downloading {total // (1024 * 1024)} MB", min(60, int(total / expected * 60)))
    if total == 0:
        raise LauncherError("Downloaded archive is empty")
    actual = digest.hexdigest()
    if release.sha256 and actual.lower() != release.sha256.lower():
        raise LauncherError("Archive SHA-256 does not match the release manifest")
    return actual


def _extract_managed(archive: Path, stage: Path, progress: Progress) -> dict[str, str]:
    staged: dict[str, str] = {}
    size_sum = 0
    with zipfile.ZipFile(archive) as z:
        infos = [x for x in z.infolist() if not x.is_dir()]
        if len(infos) > 15000:
            raise LauncherError("ZIP has too many entries")
        for info in infos:
            if info.file_size < 0:
                raise LauncherError("ZIP contains invalid file sizes")
            size_sum += info.file_size
            if size_sum > MAX_UNPACKED_BYTES:
                raise LauncherError("ZIP's expanded size is too large")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise LauncherError("ZIP contains a symlink")
            # Validate ALL paths, including ones we intend to skip.
            rel = _safe_normalize(info.filename)
            if not rel:
                continue
            if rel in staged:
                raise LauncherError(f"Duplicate normalized ZIP filename: {rel}")
            dest = stage.joinpath(*rel.split("/"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with z.open(info) as source, dest.open("wb") as target:
                n = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > info.file_size:
                        raise LauncherError("Malformed ZIP: decompressed size exceeded header")
                    target.write(chunk)
                    digest.update(chunk)
            staged[rel] = digest.hexdigest()
            progress(f"Preparing {len(staged)} pack files", 60 + min(25, int(len(staged) / max(len(infos), 1) * 25)))
    if not staged or not any(rel.startswith("mods/") for rel in staged):
        raise LauncherError("The manual ZIP contains no recognized mods. It may not be the correct archive")
    return staged


def _ensure_not_symlink(path: Path, limit: Path) -> None:
    parent = path
    while parent != limit.parent:
        if parent.is_symlink():
            raise LauncherError(f"Refusing to modify a symlinked path: {parent}")
        if parent == limit:
            break
        parent = parent.parent


def install_release(root: Path, release: Release, progress: Progress = lambda *_: None) -> dict:
    """Download, validate, stage, backup, and apply pack files, preserving saves and custom files."""
    release.validate()
    ensure_instance(root)
    old = read_installed(root)
    if old.get("version") == release.version and old.get("url") == release.url:
        progress("Already up to date", 100)
        return old
    app_dir().mkdir(parents=True, exist_ok=True)
    lock = app_dir() / "update.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as err:
        raise LauncherError("An update lock exists. Close other launchers or clear a stale update.lock") from err
    os.close(fd)
    try:
        with tempfile.TemporaryDirectory(prefix="wibsverse-update-", dir=app_dir()) as temp:
            tmp = Path(temp)
            archive = tmp / "pack.zip"
            stage = tmp / "staged"
            stage.mkdir()
            progress("Downloading modpack", 0)
            archive_hash = _download_archive(release, archive, progress)
            progress("Checking ZIP contents", 60)
            new_hashes = _extract_managed(archive, stage, progress)
            previous_files_raw = old.get("files") if isinstance(old.get("files"), dict) else {}
            previous_files = {}
            for rel, digest in previous_files_raw.items():
                if (not isinstance(rel, str) or not isinstance(digest, str)
                        or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
                        or _safe_normalize(rel) != rel):
                    raise LauncherError("Invalid installed file record; refusing to alter its paths")
                previous_files[rel] = digest
            old_files = set(previous_files)
            new_files = set(new_hashes)
            game = game_dir(root)
            # Touch only filenames that were previously owned by updater or are in this pack.
            touched = new_files | old_files
            backups = app_dir() / "backups" / f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
            backed_up: set[str] = set()
            written: set[str] = set()
            try:
                progress("Backing up replaced files", 87)
                for rel in sorted(touched):
                    target = game.joinpath(*rel.split("/"))
                    _ensure_not_symlink(target, game)
                    if target.exists():
                        backup = backups.joinpath(*rel.split("/"))
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, backup)
                        backed_up.add(rel)
                progress("Installing updated pack", 92)
                for rel in sorted(touched):
                    target = game.joinpath(*rel.split("/"))
                    if rel in new_files:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source = stage.joinpath(*rel.split("/"))
                        staged_copy = target.with_name(target.name + ".installtmp")
                        try:
                            shutil.copy2(source, staged_copy)
                            os.replace(staged_copy, target)
                        finally:
                            staged_copy.unlink(missing_ok=True)
                        written.add(rel)
                    else:
                        # Do not delete a previously managed file if user changed it.
                        if target.exists() and _sha(target) == previous_files[rel]:
                            target.unlink()
                            written.add(rel)
                new_state = {
                    "version": release.version,
                    "url": release.url,
                    "sha256": archive_hash,
                    "installed_at": int(time.time()),
                    "files": new_hashes,
                }
                atomic_json(instance_dir(root) / "wibsverse-installed.json", new_state)
            except Exception:
                # Roll back all paths that might have been changed.
                for rel in written:
                    target = game.joinpath(*rel.split("/"))
                    if rel in backed_up:
                        shutil.copy2(backups.joinpath(*rel.split("/")), target)
                    else:
                        target.unlink(missing_ok=True)
                raise
            progress("Update complete", 100)
            return new_state
    finally:
        lock.unlink(missing_ok=True)


def find_prism() -> str | None:
    paths = [
        shutil.which("prismlauncher"),
        os.path.join(os.environ.get("ProgramFiles", ""), "PrismLauncher", "prismlauncher.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "PrismLauncher", "prismlauncher.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "PrismLauncher", "prismlauncher.exe"),
    ]
    return next((str(Path(p)) for p in paths if p and Path(p).is_file()), None)


def prism_accounts(root: Path) -> list[dict[str, str]]:
    """Read display names/types only. Never expose/store Prism authentication tokens."""
    data = load_json(root / "accounts.json", {})
    out = []
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        for account in data["accounts"]:
            if not isinstance(account, dict):
                continue
            profile = account.get("profile", {})
            if not isinstance(profile, dict):
                continue
            name = profile.get("name")
            if isinstance(name, str) and name.strip():
                item = {"name": name, "type": str(account.get("type", "unknown"))}
                uuid_text = profile.get("id", "")
                if isinstance(uuid_text, str) and re.fullmatch(r"[0-9a-fA-F-]{32,36}", uuid_text):
                    item["uuid"] = uuid_text.replace("-", "").lower()
                out.append(item)
    return out


def prism_supports_offline(executable: str) -> bool:
    try:
        result = subprocess.run([executable, "--help"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=12, text=True, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "--offline" in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def validate_offline_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", name))


def prism_launch_command(executable: str, root: Path, online_name: str | None = None,
                         offline_name: str | None = None, join_server: bool = True) -> list[str]:
    if bool(online_name) == bool(offline_name):
        raise LauncherError("Choose exactly one Microsoft profile or offline username")
    command = [executable, "-d", str(root), "--launch", INSTANCE_ID]
    if online_name:
        # Only use an account Prism already knows about; it owns the credentials.
        if online_name not in {a["name"] for a in prism_accounts(root)}:
            raise LauncherError("This account isn't registered in Prism. Use Manage Accounts first")
        command.extend(["--profile", online_name])
    else:
        if not validate_offline_name(offline_name or ""):
            raise LauncherError("Offline usernames must be 3-16 letters, digits, or underscores")
        if not prism_supports_offline(executable):
            raise LauncherError("This Prism build does not advertise --offline support. Update to Prism 11.1.1")
        command.extend(["--offline", offline_name])
    if join_server and online_name:
        command.extend(["--server", SERVER])
    return command


def standard_prism_data_dir() -> Path:
    """The data root used by the installed (non-portable) Prism on Windows."""
    roaming = os.environ.get("APPDATA")
    if roaming:
        return Path(roaming) / "PrismLauncher"
    # Non-Windows development fallback; the Windows build is the target.
    return Path.home() / ".local" / "share" / "PrismLauncher"


def legacy_prism_data_dir() -> Path:
    """Wibsverse v0.1 isolated Prism root; retained for safe migration."""
    return prism_data_dir()


def migrate_legacy_instance(old_root: Path, new_root: Path, progress: Progress = lambda *_: None) -> bool:
    """Copy ONLY the Wibsverse instance to the shared root, never account tokens.

    Keep the old instance untouched as a backup. Refuse to merge with an existing
    destination instance or follow symlinks in the old instance tree.
    """
    source = instance_dir(old_root)
    target = instance_dir(new_root)
    if not source.exists():
        return False
    if target.exists():
        raise LauncherError("Wibsverse already exists in the destination Prism folder; refusing to overwrite it")
    if source.is_symlink():
        raise LauncherError("The original instance folder is a symlink; cannot migrate safely")
    paths = list(source.rglob("*"))
    for path in paths:
        if path.is_symlink():
            raise LauncherError("Legacy instance contains a symlink; migration must be done manually")
    files_total = max(1, sum(p.is_file() for p in paths))
    copied = 0

    def tracked_copy(src: str, dest: str) -> str:
        nonlocal copied
        outcome = shutil.copy2(src, dest)
        copied += 1
        if copied % 20 == 0 or copied == files_total:
            progress(f"Copying existing modpack files ({copied}/{files_total})", int(copied / files_total * 99))
        return outcome

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".migrating-" + uuid.uuid4().hex[:8])
    try:
        progress("Copying your previous Wibsverse installation...", 0)
        shutil.copytree(source, tmp, symlinks=False, copy_function=tracked_copy)
        if target.exists():
            raise LauncherError("Destination appeared during migration; refusing to overwrite it")
        tmp.rename(target)
        progress("Previous modpack successfully migrated", 100)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return True
