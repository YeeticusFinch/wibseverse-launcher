"""Public Minecraft skin lookup. No Microsoft tokens or Prism credentials are used."""
from __future__ import annotations

import base64
import binascii
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from core import LAUNCHER_VERSION

PROFILE_BASE = "https://sessionserver.mojang.com/session/minecraft/profile/"
SKIN_HOST = "textures.minecraft.net"
AVATAR_HOST = "mc-heads.net"
MAX_PROFILE_BYTES = 100_000
MAX_SKIN_BYTES = 2_000_000
CACHE_SECONDS = 24 * 60 * 60


def texture_url_from_profile(profile: dict) -> str | None:
    for prop in profile.get("properties", []):
        if not isinstance(prop, dict) or prop.get("name") != "textures":
            continue
        raw = prop.get("value", "")
        if not isinstance(raw, str) or len(raw) > 50000:
            continue
        try:
            data = json.loads(base64.b64decode(raw, validate=True))
            url = data.get("textures", {}).get("SKIN", {}).get("url")
            if not isinstance(url, str):
                continue
            parsed = urllib.parse.urlparse(url)
            # Mojang sometimes publishes an http:// link; upgrade it to HTTPS.
            if parsed.scheme not in ("http", "https") or parsed.hostname != SKIN_HOST:
                continue
            if not re.fullmatch(r"/texture/[a-fA-F0-9]{32,128}", parsed.path):
                continue
            if parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment:
                continue
            return f"https://{SKIN_HOST}{parsed.path}"
        except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error):
            continue
    return None


def _limited_urlopen(url: str, allowed_host: str, size_limit: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"WibsverseLauncher/{LAUNCHER_VERSION}"})
    with urllib.request.urlopen(req, timeout=10) as response:
        final = urllib.parse.urlparse(response.geturl())
        if final.scheme != "https" or final.hostname != allowed_host:
            raise ValueError("Unexpected redirect in Minecraft skin lookup")
        content = response.read(size_limit + 1)
    if len(content) > size_limit:
        raise ValueError("Skin response too large")
    return content


def skin_png_for_uuid(uuid_text: str, cache: Path) -> bytes | None:
    uuid_text = uuid_text.replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", uuid_text):
        return None
    path = cache / (uuid_text + ".png")
    if path.is_file() and (time.time() - path.stat().st_mtime < CACHE_SECONDS):
        return path.read_bytes()
    data = None
    try:
        profile_raw = _limited_urlopen(PROFILE_BASE + uuid_text, "sessionserver.mojang.com", MAX_PROFILE_BYTES)
        skin_url = texture_url_from_profile(json.loads(profile_raw))
        if skin_url:
            data = _limited_urlopen(skin_url, SKIN_HOST, MAX_SKIN_BYTES)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, urllib.error.URLError):
        # The session profile can outlive its texture URL. Use a rendered public head below.
        data = None
    if data is None:
        data = _limited_urlopen(
            f"https://{AVATAR_HOST}/avatar/{uuid_text}/40.png?overlay",
            AVATAR_HOST,
            MAX_SKIN_BYTES,
        )
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Minecraft skin service returned a non-PNG image")
    cache.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return data
