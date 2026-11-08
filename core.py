"""Wibsverse Launcher: HeadlessMc integration and safe modpack updater."""
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
import urllib.error
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
HMC_PROFILE_NAME = "wibsverse"
HMC_ROOT_NAME = "HeadlessMc"
HMC_VERSION = "3.0.0-RC8"
HMC_ASSET = "headlessmc-launcher-windows-x64.exe"
HMC_DOWNLOAD_URL = (
    "https://github.com/headlesshq/headlessmc/releases/download/"
    f"{HMC_VERSION}/{HMC_ASSET}"
)
HMC_SHA256 = "72c42ddd7240f08c79615bbf5e9bef15d209d3e75017f4ef9d9dde9f5028f935"
JAVA_VERSION = 17
JAVA_METADATA_URL = (
    "https://api.adoptium.net/v3/assets/latest/17/hotspot?"
    "architecture=x64&image_type=jre&os=windows&vendor=eclipse"
)
MAX_JAVA_METADATA_BYTES = 1 * 1024 * 1024
MAX_JAVA_DOWNLOAD_BYTES = 150 * 1024 * 1024
MAX_JAVA_UNPACKED_BYTES = 600 * 1024 * 1024
MAX_HMC_DOWNLOAD_BYTES = 150 * 1024 * 1024
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


def headlessmc_root() -> Path:
    return app_dir() / HMC_ROOT_NAME


def headlessmc_executable_path(root: Path | None = None) -> Path:
    return (root or headlessmc_root()) / "bin" / "headlessmc.exe"


def instance_dir(root: Path) -> Path:
    """Return the wrapper-owned HMC installation root."""
    return root


def game_dir(root: Path) -> Path:
    return root / "game"


def hmc_data_dir(root: Path) -> Path:
    return root / "data" / "headlessmc"


def hmc_config_file(root: Path) -> Path:
    return root / "config" / "headlessmc" / "config.properties"


def hmc_environment(root: Path) -> dict[str, str]:
    """Give HMC isolated XDG directories, including on Windows."""
    env = os.environ.copy()
    env.update({
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_CACHE_HOME": str(root / "cache"),
    })
    return env


def _hmc_path_value(path: Path) -> str:
    # Java properties treat backslashes as escapes. Forward slashes are valid on Windows.
    return str(path.resolve()).replace("\\", "/")


def ensure_hmc_config(root: Path, java_home: Path | None = None) -> None:
    """Configure HMC without changing the user's normal HeadlessMc installation."""
    path = hmc_config_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    required = {
        "hmc.files.mc": _hmc_path_value(root / "minecraft"),
        "hmc.files.game": _hmc_path_value(game_dir(root)),
        "hmc.files.game-for-each-version": "false",
        "hmc.jline.enabled": "false",
        "hmc.java.download": "false",
        "hmc.updater.enabled": "false",
    }
    if java_home is not None:
        required["hmc.java.versions"] = _hmc_path_value(java_home)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keys = set(required) | {
        "hmc.launcher.auto-download-specifics",
        "hmc.java.check-for-updates",
        "hmc.java.versions",
        "hmc.java.versions",
    }
    lines = [line for line in existing if not line or line.lstrip().startswith("#")
             or line.split("=", 1)[0].strip() not in keys]
    lines.extend(f"{key}={value}" for key, value in required.items())
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


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
    data = load_json(root / "wibsverse-installed.json", {})
    return data if isinstance(data, dict) else {}


def ensure_instance(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    game_dir(root).mkdir(parents=True, exist_ok=True)
    ensure_hmc_config(root)


def _limited_urlopen(url: str, allowed_host: str, size_limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "WibsverseLauncher/0.3"})
    with urllib.request.urlopen(request, timeout=30) as response:
        final = urllib.parse.urlparse(response.geturl())
        if final.scheme != "https" or final.hostname != allowed_host:
            raise LauncherError("Unexpected redirect while downloading setup metadata")
        content = response.read(size_limit + 1)
    if len(content) > size_limit:
        raise LauncherError("Setup metadata response is too large")
    return content


def _java_executable(home: Path) -> Path | None:
    for name in ("java.exe", "java"):
        executable = home / "bin" / name
        if executable.is_file():
            return executable
    return None


def _java_major(home: Path) -> int | None:
    executable = _java_executable(home)
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [str(executable), "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'(?:version\s+|openjdk\s+)["\']?(\d+)', result.stdout or "", re.I)
    return int(match.group(1)) if match else None


def find_java_home(root: Path, major: int = JAVA_VERSION) -> Path | None:
    """Find a Java home HMC can use, preferring the isolated launcher runtime."""
    candidates: list[Path] = []
    java_dir = hmc_data_dir(root) / "java"
    if java_dir.is_dir():
        candidates.extend(path for path in sorted(java_dir.iterdir()) if path.is_dir())
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(Path(java_home))
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        system_java = Path(program_files) / "Java"
        if system_java.is_dir():
            candidates.extend(path for path in sorted(system_java.iterdir()) if path.is_dir())
    path_java = shutil.which("java")
    if path_java:
        candidates.append(Path(path_java).resolve().parent.parent)
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        if _java_major(candidate) == major:
            return candidate
    return None


def _java_archive_path(name: str) -> Path:
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise LauncherError(f"Unsafe Java archive path: {name}")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or any("\x00" in part or ":" in part for part in parts):
        raise LauncherError(f"Unsafe Java archive path: {name}")
    return Path(*parts)


def _download_verified_file(
    url: str,
    destination: Path,
    allowed_hosts: tuple[str, ...],
    maximum: int,
    expected_sha256: str,
    progress: Progress = lambda *_: None,
) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise LauncherError("Java download URL uses an unexpected host")
    request = urllib.request.Request(url, headers={"User-Agent": "WibsverseLauncher/0.3"})
    digest = hashlib.sha256()
    total = 0
    temporary = destination.with_name(destination.name + ".download")
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as target:
            final = urllib.parse.urlparse(response.geturl())
            final_host = final.hostname or ""
            if final.scheme != "https" or not (
                final_host in allowed_hosts or final_host.endswith(".githubusercontent.com")
            ):
                raise LauncherError("Java download redirected to an unexpected host")
            expected_size = int(response.headers.get("Content-Length", 0))
            if expected_size > maximum:
                raise LauncherError("Java download exceeds the size limit")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise LauncherError("Java download exceeds the size limit")
                target.write(chunk)
                digest.update(chunk)
                progress("Downloading Java 17...", min(90, total * 90 // max(expected_size, total)))
        if total == 0 or digest.hexdigest().lower() != expected_sha256.lower():
            raise LauncherError("Java 17 download SHA-256 does not match the release metadata")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _extract_java_archive(archive: Path, destination: Path) -> Path:
    stage = destination.parent / (".java-stage-" + uuid.uuid4().hex)
    extracted = stage / "extracted"
    total = 0
    try:
        extracted.mkdir(parents=True)
        with zipfile.ZipFile(archive) as z:
            infos = [info for info in z.infolist() if not info.is_dir()]
            if len(infos) > 10000:
                raise LauncherError("Java archive has too many entries")
            for info in infos:
                rel = _java_archive_path(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise LauncherError("Java archive contains a symlink")
                total += info.file_size
                if total > MAX_JAVA_UNPACKED_BYTES:
                    raise LauncherError("Java archive expands beyond the size limit")
                target = extracted.joinpath(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
        homes = sorted({path.parent.parent for path in extracted.rglob("java.exe") if path.parent.name == "bin"})
        if not homes:
            raise LauncherError("Java 17 archive does not contain bin\\java.exe")
        home = homes[0]
        if destination.exists():
            raise LauncherError("A managed Java 17 installation already exists")
        os.replace(home, destination)
        return destination
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def ensure_java17(root: Path, progress: Progress = lambda *_: None) -> Path:
    """Ensure an isolated Java 17 runtime exists and is selected by HMC."""
    ensure_instance(root)
    existing = find_java_home(root)
    if existing is not None:
        managed_root = hmc_data_dir(root) / "java"
        if managed_root not in existing.parents:
            ensure_hmc_config(root, existing)
        else:
            ensure_hmc_config(root)
        return existing

    progress("Java 17 was not found. Downloading a private Temurin runtime...", 0)
    try:
        metadata = json.loads(_limited_urlopen(JAVA_METADATA_URL, "api.adoptium.net", MAX_JAVA_METADATA_BYTES))
        entry = next(
            item for item in metadata if isinstance(item, dict)
            and isinstance(item.get("binary"), dict)
            and isinstance(item["binary"].get("package"), dict)
        )
        package = entry["binary"]["package"]
        url = package.get("link")
        checksum = package.get("checksum")
        if not isinstance(url, str) or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
            raise LauncherError("Adoptium returned incomplete Java 17 package metadata")
        java_dir = hmc_data_dir(root) / "java"
        java_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="wibsverse-java-", dir=str(root)) as temporary:
            archive = Path(temporary) / "java17.zip"
            _download_verified_file(
                url, archive, ("github.com", "api.adoptium.net"), MAX_JAVA_DOWNLOAD_BYTES, checksum,
                lambda message, value: progress(message, value),
            )
            home = _extract_java_archive(archive, java_dir / "wibsverse-temurin-17")
        if _java_major(home) != JAVA_VERSION:
            raise LauncherError("Downloaded Java runtime is not Java 17")
        ensure_hmc_config(root)
        progress("Java 17 is ready", 100)
        return home
    except (OSError, ValueError, TypeError, KeyError, StopIteration, json.JSONDecodeError, zipfile.BadZipFile,
            urllib.error.URLError) as error:
        raise LauncherError(f"Could not install Java 17: {error}") from error


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


def find_headlessmc() -> str | None:
    paths = [
        shutil.which("headlessmc"),
        shutil.which("headlessmc.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "headlessmc", "bin", "headlessmc.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "headlessmc", "headlessmc.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "HeadlessMc", "headlessmc.exe"),
        str(headlessmc_executable_path()),
        str(app_dir() / "headlessmc.exe"),
    ]
    return next((str(Path(p)) for p in paths if p and Path(p).is_file()), None)


def install_headlessmc(root: Path, progress: Progress = lambda *_: None) -> Path:
    """Download the pinned official Windows binary into the launcher data directory."""
    destination = headlessmc_executable_path(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".download")
    request = urllib.request.Request(
        HMC_DOWNLOAD_URL,
        headers={"User-Agent": "WibsverseLauncher/0.3", "Accept": "application/octet-stream"},
    )
    digest = hashlib.sha256()
    total = 0
    try:
        progress(f"Downloading HeadlessMc {HMC_VERSION}...", 0)
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as target:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or not (
                final.hostname == "github.com" or (final.hostname or "").endswith("githubusercontent.com")
            ):
                raise LauncherError("HeadlessMc download redirected to an unexpected host")
            expected = int(response.headers.get("Content-Length", 0))
            if expected > MAX_HMC_DOWNLOAD_BYTES:
                raise LauncherError("HeadlessMc download exceeds the size limit")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_HMC_DOWNLOAD_BYTES:
                    raise LauncherError("HeadlessMc download exceeds the size limit")
                digest.update(chunk)
                target.write(chunk)
                progress("Downloading HeadlessMc...", min(90, total * 90 // max(expected, total)))
        if total == 0:
            raise LauncherError("HeadlessMc download was empty")
        if digest.hexdigest().lower() != HMC_SHA256:
            raise LauncherError("HeadlessMc download SHA-256 does not match the pinned release")
        os.replace(temporary, destination)
        progress(f"HeadlessMc {HMC_VERSION} installed", 100)
        return destination
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise LauncherError(f"Could not download HeadlessMc: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def hmc_command(executable: str, *arguments: str) -> list[str]:
    return [executable, *arguments]


def hmc_login_command(executable: str) -> list[str]:
    return hmc_command(executable, "account", "--provider", "default", "login")


def hmc_account_list_command(executable: str) -> list[str]:
    return hmc_command(executable, "account", "list")


def hmc_offline_login_command(executable: str) -> list[str]:
    return hmc_command(executable, "account", "--provider", "offline", "login")


def hmc_offline_accounts_path(root: Path) -> Path:
    return hmc_data_dir(root) / ".auth" / "offline" / ".accounts.json"


def create_hmc_offline_account(root: Path, name: str) -> dict[str, str]:
    """Add a tokenless offline account using HMC's versioned account-store schema."""
    name = name.strip()
    if not name or any(char in name for char in "\r\n\x00"):
        raise LauncherError("Enter a valid offline account name")
    path = hmc_offline_accounts_path(root)
    try:
        data = load_json(path, {})
    except (OSError, ValueError, TypeError) as error:
        raise LauncherError(f"Could not read offline accounts: {error}") from error
    if not isinstance(data, dict):
        raise LauncherError("HMC offline account store is invalid")
    accounts = data.get("accounts", {})
    if accounts is None:
        accounts = {}
    if not isinstance(accounts, dict):
        raise LauncherError("HMC offline account store is invalid")
    if any(isinstance(existing, str) and existing.casefold() == name.casefold() for existing in accounts):
        raise LauncherError(f"An account named {name} already exists")
    account = {
        "provider": "offline",
        "name": name,
        "uuid": str(uuid.uuid4()),
        "token": "",
        "type": "msa",
        "xuid": "",
    }
    accounts[name] = account
    atomic_json(path, {"accounts": accounts, "version": 0})
    return account


def hmc_remove_account_command(executable: str, name: str) -> list[str]:
    if not name or any(char in name for char in "\r\n\x00"):
        raise LauncherError("Invalid account name")
    return hmc_command(executable, "account", "remove", name)


def hmc_select_account_command(executable: str, name: str) -> list[str]:
    if not name or any(char in name for char in "\r\n\x00"):
        raise LauncherError("Invalid Microsoft account name")
    return hmc_command(executable, "account", "select", name)


def hmc_profile_path(root: Path) -> Path:
    return root / "config" / "headlessmc" / "profiles" / (HMC_PROFILE_NAME + ".json")


def hmc_profile_command(executable: str, root: Path) -> list[str]:
    return hmc_command(
        executable, "profile", "add", "forge", MINECRAFT_VERSION, FORGE_VERSION,
        "--force", "--name", HMC_PROFILE_NAME, "--dir", str(game_dir(root)),
    )


def hmc_launch_command(executable: str, account_name: str, join_server: bool = True) -> list[str]:
    if not account_name or any(char in account_name for char in "\r\n\x00"):
        raise LauncherError("Choose a Microsoft account before launching")
    # HMC itself runs as a hidden subprocess; omit HMC's --headless flag so Minecraft
    # keeps its normal graphical client for gameplay.
    command = hmc_command(executable, "launch")
    if join_server:
        command.extend(["--server", SERVER])
    command.append(HMC_PROFILE_NAME)
    return command


def _profile_version_matches(value: object) -> bool:
    if isinstance(value, dict):
        platform = value.get("platform")
        version = value.get("version")
        build = value.get("build")
        if isinstance(build, dict):
            build = build.get("value") or build.get("orElse")
        if isinstance(build, list) and len(build) == 1:
            build = build[0]
        return platform == "forge" and version == MINECRAFT_VERSION and build == FORGE_VERSION
    if isinstance(value, str):
        return value.lower() in {
            f"forge/{MINECRAFT_VERSION}/{FORGE_VERSION}".lower(),
            f"forge-{MINECRAFT_VERSION}-{FORGE_VERSION}".lower(),
        }
    return False


def hmc_profile_is_pinned(root: Path) -> bool:
    data = load_json(hmc_profile_path(root), {})
    if not isinstance(data, dict):
        return False
    return _profile_version_matches(data.get("currentVersion")) or _profile_version_matches(data.get("version"))


def run_hmc(
    executable: str,
    root: Path,
    arguments: list[str],
    timeout: int = 30,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    ensure_instance(root)
    try:
        result = subprocess.run(
            hmc_command(executable, *arguments),
            env=hmc_environment(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LauncherError(f"HeadlessMC command failed: {error}") from error
    if result.returncode != 0:
        output = result.stdout.strip()
        detail = output[-1200:] if output else f"exit code {result.returncode}"
        raise LauncherError(f"HeadlessMC command failed: {detail}")
    return result


def parse_hmc_accounts(output: str) -> list[dict[str, str]]:
    """Parse HMC's public account table without touching its token store."""
    accounts: list[dict[str, str]] = []
    uuid_re = re.compile(r"^[0-9a-fA-F-]{32,36}$")
    ansi_re = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    for line in output.splitlines():
        line = ansi_re.sub("", line)
        columns = re.split(r"\s{2,}", line.strip())
        if len(columns) < 4 or columns[0].lower() == "name":
            continue
        name, uuid_text, account_type, provider = columns[:4]
        if not name or not uuid_re.fullmatch(uuid_text):
            continue
        if account_type.lower() not in {"msa", "microsoft"}:
            continue
        accounts.append({
            "mode": "offline" if provider.lower() == "offline" else "microsoft",
            "name": name,
            "uuid": uuid_text.replace("-", "").lower(),
            "provider": provider,
        })
    return accounts


def hmc_accounts(executable: str, root: Path) -> list[dict[str, str]]:
    return parse_hmc_accounts(run_hmc(executable, root, hmc_account_list_command(executable)[1:]).stdout)


def legacy_prism_data_dir() -> Path:
    """The isolated Prism root used by the previous wrapper release."""
    return app_dir() / "PrismData"


def _prism_instance_dir(root: Path) -> Path:
    return root / "instances" / INSTANCE_ID


def migrate_legacy_instance(old_root: Path, new_root: Path, progress: Progress = lambda *_: None) -> bool:
    """Copy only old Prism game files into the HMC game directory."""
    source_instance = _prism_instance_dir(old_root)
    source = source_instance / ".minecraft"
    target = game_dir(new_root)
    if not source.exists():
        return False
    if target.exists() and any(target.iterdir()):
        raise LauncherError("The HMC game directory already contains files; refusing to overwrite it")
    if source.is_symlink():
        raise LauncherError("The old Prism game directory is a symlink; cannot migrate safely")
    paths = list(source.rglob("*"))
    for path in paths:
        if path.is_symlink():
            raise LauncherError("The old Prism game directory contains a symlink; migration must be done manually")
    files_total = max(1, sum(p.is_file() for p in paths))
    copied = 0

    def tracked_copy(src: str, dest: str) -> str:
        nonlocal copied
        outcome = shutil.copy2(src, dest)
        copied += 1
        if copied % 20 == 0 or copied == files_total:
            progress(f"Copying existing Wibsverse files ({copied}/{files_total})", int(copied / files_total * 99))
        return outcome

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".migrating-" + uuid.uuid4().hex[:8])
    try:
        progress("Copying your previous Wibsverse installation...", 0)
        shutil.copytree(source, tmp, symlinks=False, copy_function=tracked_copy)
        if target.exists():
            raise LauncherError("Destination appeared during migration; refusing to overwrite it")
        tmp.rename(target)
        state = source_instance / "wibsverse-installed.json"
        if state.is_file():
            shutil.copy2(state, new_root / "wibsverse-installed.json")
        progress("Previous modpack successfully migrated", 100)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return True
