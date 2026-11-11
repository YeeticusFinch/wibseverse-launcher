"""Secure file handoff contract used by WibsVerse SSO 1.0.0."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from wibsverse_auth import GameAuthCredential

CONTEXT_FIELDS = {
    "version", "session_id", "nonce", "process_id", "runtime_uuid", "minecraft_username",
    "target_host", "target_port", "created_at_epoch_seconds", "expires_at_epoch_seconds", "context_sha256",
}
CREDENTIAL_FIELDS = {
    "version", "session_id", "nonce", "context_sha256", "process_id", "handoff_id", "ott",
    "runtime_uuid", "minecraft_username", "proof_signature", "expires_at_epoch_seconds",
}


class SsoHandoffError(RuntimeError):
    pass


def context_digest(
    session_id: str,
    nonce: str,
    process_id: int,
    runtime_uuid: str,
    minecraft_username: str,
    target_host: str,
    target_port: int,
    created_at_epoch_seconds: int,
    expires_at_epoch_seconds: int,
) -> str:
    canonical = "WVLAUNCHIPC1\n" + "\n".join(map(str, (
        session_id, nonce, process_id, runtime_uuid, minecraft_username,
        target_host, target_port, created_at_epoch_seconds, expires_at_epoch_seconds,
    )))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: dict):
    temporary = path.with_name(path.name + ".tmp")
    data = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class SsoHandoff:
    directory: Path
    session_id: str
    nonce: str
    runtime_uuid: str
    username: str
    target_host: str
    target_port: int
    expires_at_epoch_seconds: int
    context_sha256: str = ""
    link_code_expires_at_epoch_seconds: int = 0

    @classmethod
    def create(
        cls,
        root: Path,
        runtime_uuid: str,
        username: str,
        target_host: str,
        target_port: int = 25565,
        base: Path | None = None,
    ) -> "SsoHandoff":
        try:
            runtime_uuid = str(uuid.UUID(runtime_uuid))
        except (ValueError, AttributeError) as error:
            raise SsoHandoffError("The Minecraft account UUID is invalid for SSO.") from error
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", username):
            raise SsoHandoffError("The Minecraft username is invalid for SSO.")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,58}[a-z0-9])?", target_host.lower()):
            raise SsoHandoffError("The WibsVerse target host is invalid.")
        if not 1 <= target_port <= 65535:
            raise SsoHandoffError("The WibsVerse target port is invalid.")
        session = str(uuid.uuid4())
        directory = (base or Path(__import__("tempfile").gettempdir())) / f"wibsverse-sso-{session}"
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        if os.name == "nt":
            domain = os.environ.get("USERDOMAIN", "")
            user = os.environ.get("USERNAME", "")
            principal = f"{domain}\\{user}" if domain else user
            if not principal:
                directory.rmdir()
                raise SsoHandoffError("Could not determine the current Windows account for SSO IPC security.")
            try:
                result = subprocess.run(
                    ["icacls", str(directory), "/inheritance:r", "/grant:r", f"{principal}:(OI)(CI)F"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=10, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                directory.rmdir()
                raise SsoHandoffError("Could not secure the WibsVerse SSO handoff directory.") from error
            if result.returncode != 0:
                directory.rmdir()
                raise SsoHandoffError("Could not secure the WibsVerse SSO handoff directory.")
        else:
            directory.chmod(0o700)
        now = int(time.time())
        nonce = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
        expiry = now + 60
        digest = context_digest(session, nonce, 0, runtime_uuid.lower(), username, target_host.lower(), target_port, now, expiry)
        return cls(directory, session, nonce, runtime_uuid.lower(), username, target_host.lower(), target_port, expiry, digest)

    @property
    def java_property(self) -> str:
        return str(self.directory.resolve()).replace("\\", "/")

    def write_context(self, process_id: int):
        now = int(time.time())
        expiry = now + 60
        self.expires_at_epoch_seconds = expiry
        if process_id <= 0:
            raise SsoHandoffError("The Minecraft process ID is invalid for SSO IPC.")
        self.context_sha256 = context_digest(
            self.session_id, self.nonce, process_id, self.runtime_uuid, self.username,
            self.target_host, self.target_port, now, expiry,
        )
        payload = {
            "version": 1,
            "session_id": self.session_id,
            "nonce": self.nonce,
            "process_id": process_id,
            "runtime_uuid": self.runtime_uuid,
            "minecraft_username": self.username,
            "target_host": self.target_host,
            "target_port": self.target_port,
            "created_at_epoch_seconds": now,
            "expires_at_epoch_seconds": expiry,
            "context_sha256": self.context_sha256,
        }
        if set(payload) != CONTEXT_FIELDS:
            raise SsoHandoffError("Internal SSO context fields do not match the installed mod contract.")
        _write_json(self.directory / "context.json", payload)

    def wait_until_ready(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        ready_path = self.directory / "ready.json"
        while time.monotonic() < deadline:
            if ready_path.is_file():
                try:
                    ready = json.loads(ready_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return False
                return isinstance(ready, dict) and ready.get("session_id") == self.session_id
            time.sleep(0.1)
        return False

    def wait_until_consumed(self, timeout: float = 30.0) -> bool:
        marker = self.directory / "consumed.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker.is_file():
                try:
                    consumed = json.loads(marker.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return False
                return isinstance(consumed, dict) and consumed.get("session_id") == self.session_id
            time.sleep(0.1)
        return False

    def write_credential(self, credential: GameAuthCredential):
        if credential.runtime_uuid.lower() != self.runtime_uuid or credential.minecraft_username != self.username:
            raise SsoHandoffError("The WibsVerse login credential does not match the selected Minecraft account.")
        if not (self.directory / "ready.json").is_file():
            raise SsoHandoffError("WibsVerse SSO has not prepared its launcher handoff yet.")
        payload = {
            "version": 1,
            "session_id": self.session_id,
            "nonce": self.nonce,
            "context_sha256": self.context_sha256,
            "process_id": None,
            "handoff_id": self.directory.name,
            "ott": credential.ott,
            "runtime_uuid": credential.runtime_uuid.lower(),
            "minecraft_username": credential.minecraft_username,
            "proof_signature": credential.proof_signature,
            "expires_at_epoch_seconds": credential.expires_at_epoch_seconds,
        }
        # The exact process ID is copied from the authenticated context written immediately before.
        context = json.loads((self.directory / "context.json").read_text(encoding="utf-8"))
        payload["process_id"] = context["process_id"]
        if set(payload) != CREDENTIAL_FIELDS:
            raise SsoHandoffError("Internal SSO credential fields do not match the installed mod contract.")
        _write_json(self.directory / "credential.json", payload)

    def write_link_code(self, code: str, expires_at: datetime):
        if not re.fullmatch(r"[A-Z0-9]{4}(?:-[A-Z0-9]{4}){3}", code):
            raise SsoHandoffError("WibsVerse returned an invalid Minecraft link code.")
        expiry = int(expires_at.astimezone(timezone.utc).timestamp())
        if expiry <= int(time.time()):
            raise SsoHandoffError("The WibsVerse Minecraft link code expired before launch.")
        _write_json(self.directory / "link-code.json", {
            "session_id": self.session_id,
            "code": code,
            "minecraft_username": self.username,
            "runtime_uuid": self.runtime_uuid,
            "expires_at_epoch_seconds": expiry,
        })
        self.link_code_expires_at_epoch_seconds = expiry

    def wait_link_command(self, timeout: float = 25.0) -> bool:
        result_path = self.directory / "link-status.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result_path.is_file():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return False
                return (
                    isinstance(result, dict)
                    and result.get("session_id") == self.session_id
                    and result.get("status") == "command_sent"
                    and result.get("minecraft_username") == self.username
                )
            time.sleep(0.2)
        return False

    def cleanup(self):
        for name in ("context.json", "ready.json", "credential.json", "consumed.json", "link-code.json", "link-status.json"):
            (self.directory / name).unlink(missing_ok=True)
        try:
            self.directory.rmdir()
        except OSError:
            pass

    def cleanup_auth_files(self):
        for name in ("context.json", "ready.json", "credential.json", "consumed.json"):
            (self.directory / name).unlink(missing_ok=True)
