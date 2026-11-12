"""Custom client mod inventory and Modrinth API helpers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from core import LauncherError, atomic_json, game_dir, read_installed

MODRINTH_API = "https://api.modrinth.com/v2"
GAME_VERSION = "1.20.1"
LOADER = "forge"
MAX_MOD_DOWNLOAD = 512 * 1024 * 1024
MAX_API_RESPONSE = 12 * 1024 * 1024
USER_AGENT = "WibsverseLauncher/0.4 (client-mod-manager)"


@dataclass(frozen=True)
class ClientMod:
    path: Path
    enabled: bool
    project_id: str = ""
    version_id: str = ""
    version_number: str = ""
    sha1: str = ""

    @property
    def filename(self) -> str:
        name = self.path.name
        return name[:-len(".disabled")] if name.casefold().endswith(".disabled") else name


def mods_state_path(root: Path) -> Path:
    return root / "custom-client-mods.json"


def _load_mod_metadata(root: Path) -> dict[str, dict]:
    try:
        data = json.loads(mods_state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    mods = data.get("mods", {}) if isinstance(data, dict) else {}
    return {str(name).casefold(): value for name, value in mods.items() if isinstance(value, dict)} if isinstance(mods, dict) else {}


def _save_mod_metadata(root: Path, metadata: dict[str, dict]) -> None:
    atomic_json(mods_state_path(root), {"mods": metadata})


def _manifest_mod_names(installed: dict) -> set[str]:
    files = installed.get("files", {}) if isinstance(installed, dict) else {}
    if not isinstance(files, dict):
        return set()
    names = set()
    for rel in files:
        if isinstance(rel, str) and rel.replace("\\", "/").lower().startswith("mods/"):
            name = Path(rel.replace("\\", "/")).name.casefold()
            if name.endswith(".jar"):
                names.add(name)
    return names


def _mod_sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mod_sha1(path: Path) -> str:
    return _mod_sha1(path)


def list_custom_mods(root: Path, installed: dict) -> list[ClientMod]:
    """List non-pack JARs on disk, accounting for disabled pack JARs."""
    folder = game_dir(root) / "mods"
    folder.mkdir(parents=True, exist_ok=True)
    pack_names = _manifest_mod_names(installed)
    metadata = _load_mod_metadata(root)
    result = []
    for path in sorted(folder.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file():
            continue
        lower = path.name.casefold()
        enabled = lower.endswith(".jar")
        if lower.endswith(".jar.disabled"):
            base_name = path.name[:-len(".disabled")]
        elif enabled:
            base_name = path.name
        else:
            continue
        if base_name.casefold() in pack_names:
            continue
        record = metadata.get(base_name.casefold(), {})
        result.append(ClientMod(
            path=path,
            enabled=enabled,
            project_id=str(record.get("project_id") or ""),
            version_id=str(record.get("version_id") or ""),
            version_number=str(record.get("version_number") or ""),
            sha1=str(record.get("sha1") or ""),
        ))
    return result


def set_mod_enabled(root: Path, mod: ClientMod, enabled: bool) -> ClientMod:
    if mod.enabled == enabled:
        return mod
    target = mod.path.with_name(mod.filename if enabled else mod.filename + ".disabled")
    if target.exists():
        raise LauncherError(f"Cannot rename mod because {target.name} already exists")
    os.replace(mod.path, target)
    return ClientMod(target, enabled, mod.project_id, mod.version_id, mod.version_number, mod.sha1)


def delete_mod(root: Path, mod: ClientMod) -> None:
    mod.path.unlink()
    metadata = _load_mod_metadata(root)
    metadata.pop(mod.filename.casefold(), None)
    _save_mod_metadata(root, metadata)


def _api_json(url: str, timeout: int = 18):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname != "api.modrinth.com":
                raise LauncherError("Modrinth API redirected to an unexpected host")
            payload = response.read(MAX_API_RESPONSE + 1)
    except (OSError, urllib.error.URLError) as error:
        raise LauncherError(f"Could not contact Modrinth: {error}") from error
    if len(payload) > MAX_API_RESPONSE:
        raise LauncherError("Modrinth returned an unexpectedly large response")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LauncherError("Modrinth returned invalid JSON") from error


def search_modrinth(query: str, limit: int = 30) -> list[dict]:
    facets = [["project_type:mod"], [f"versions:{GAME_VERSION}"], [f"categories:{LOADER}"]]
    params = urllib.parse.urlencode({"query": query, "limit": min(max(limit, 1), 100), "facets": json.dumps(facets)})
    data = _api_json(f"{MODRINTH_API}/search?{params}")
    hits = data.get("hits", []) if isinstance(data, dict) else []
    return [hit for hit in hits if isinstance(hit, dict)]


def get_project(project_id: str) -> dict:
    return _api_json(f"{MODRINTH_API}/project/{urllib.parse.quote(project_id, safe='')}")


def get_project_versions(project_id: str) -> list[dict]:
    params = urllib.parse.urlencode({"game_versions": json.dumps([GAME_VERSION]), "loaders": json.dumps([LOADER])})
    data = _api_json(f"{MODRINTH_API}/project/{urllib.parse.quote(project_id, safe='')}/version?{params}")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def get_project_dependencies(project_id: str) -> dict:
    data = _api_json(f"{MODRINTH_API}/project/{urllib.parse.quote(project_id, safe='')}/dependencies")
    return data if isinstance(data, dict) else {}


def lookup_modrinth_hash(sha1: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha1):
        return None
    encoded = urllib.parse.quote(sha1.lower(), safe="")
    try:
        data = _api_json(f"{MODRINTH_API}/version_file/{encoded}?algorithm=sha1")
    except LauncherError as error:
        if "404" in str(error):
            return None
        raise
    return data if isinstance(data, dict) else None


def remember_modrinth_version(root: Path, filename: str, version: dict, sha1: str) -> None:
    metadata = _load_mod_metadata(root)
    metadata[filename.casefold()] = {
        "project_id": str(version.get("project_id") or ""),
        "version_id": str(version.get("id") or ""),
        "version_number": str(version.get("version_number") or ""),
        "sha1": sha1,
    }
    _save_mod_metadata(root, metadata)


def _safe_jar_filename(filename: str) -> str:
    name = Path(filename).name
    if name != filename or not name.lower().endswith(".jar") or any(c in name for c in ("\x00", "/", "\\", ":")):
        raise LauncherError("Modrinth returned an unsafe JAR filename")
    if name.casefold().endswith(".jar.disabled"):
        raise LauncherError("Invalid JAR filename")
    return name


def _download_jar(url: str, destination: Path, expected_sha1: str = "") -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "cdn.modrinth.com":
        raise LauncherError("Modrinth file URL uses an unexpected host")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    temp = destination.with_name(destination.name + ".download-" + uuid.uuid4().hex)
    digest = hashlib.sha1()
    count = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temp.open("wb") as output:
            final = urllib.parse.urlparse(response.geturl())
            host = final.hostname or ""
            if final.scheme != "https" or not (host == "cdn.modrinth.com" or host.endswith(".modrinth.com")):
                raise LauncherError("Modrinth download redirected to an unexpected host")
            length = int(response.headers.get("Content-Length", 0))
            if length > MAX_MOD_DOWNLOAD:
                raise LauncherError("Mod download exceeds the size limit")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_MOD_DOWNLOAD:
                    raise LauncherError("Mod download exceeds the size limit")
                output.write(chunk)
                digest.update(chunk)
        if not count or not zipfile_is_jar(temp):
            raise LauncherError("The downloaded file is not a valid JAR")
        if expected_sha1 and digest.hexdigest().casefold() != expected_sha1.casefold():
            raise LauncherError("Downloaded mod SHA-1 does not match Modrinth metadata")
        os.replace(temp, destination)
        return digest.hexdigest()
    finally:
        temp.unlink(missing_ok=True)


def zipfile_is_jar(path: Path) -> bool:
    import zipfile
    return zipfile.is_zipfile(path)


def install_modrinth_version(root: Path, project: dict, version: dict, replace: ClientMod | None = None) -> ClientMod:
    files = version.get("files", [])
    jars = [item for item in files if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(".jar")]
    if not jars:
        raise LauncherError("This Modrinth version does not include a JAR file")
    file_info = next((item for item in jars if item.get("primary")), jars[0])
    filename = _safe_jar_filename(str(file_info.get("filename") or ""))
    target = (replace.path.parent / filename) if replace else game_dir(root) / "mods" / filename
    if target.exists() and (replace is None or target != replace.path):
        raise LauncherError(f"A mod named {filename} already exists")
    if replace is None:
        if target.with_name(target.name + ".disabled").exists():
            raise LauncherError(f"A disabled mod named {filename} already exists")
        if filename.casefold() in _manifest_mod_names(read_installed(root)):
            raise LauncherError("This file is already part of the Wibsverse modpack")
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = file_info.get("hashes", {}).get("sha1") if isinstance(file_info.get("hashes"), dict) else None
    sha1 = _download_jar(str(file_info.get("url") or ""), target, str(expected or ""))
    if replace and replace.path != target:
        replace.path.unlink(missing_ok=True)
    if replace and not replace.enabled:
        disabled_target = target.with_name(target.name + ".disabled")
        if disabled_target.exists():
            target.unlink(missing_ok=True)
            raise LauncherError(f"Cannot preserve disabled state because {disabled_target.name} already exists")
        os.replace(target, disabled_target)
        target = disabled_target
    metadata = _load_mod_metadata(root)
    if replace:
        metadata.pop(replace.filename.casefold(), None)
    metadata[filename.casefold()] = {
        "project_id": str(project.get("id") or version.get("project_id") or ""),
        "version_id": str(version.get("id") or ""),
        "version_number": str(version.get("version_number") or ""),
        "sha1": sha1,
    }
    _save_mod_metadata(root, metadata)
    return ClientMod(target, target.name.casefold().endswith(".jar"), metadata[filename.casefold()]["project_id"], metadata[filename.casefold()]["version_id"], metadata[filename.casefold()]["version_number"], sha1)
