"""WibsVerse web login and the WibsVerse SSO game's short-lived auth ticket."""
from __future__ import annotations

import base64
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable

SITE_ORIGIN = "https://www.wibsverse.com"
GAME_AUTH_ORIGIN = "https://wibsverse.com/api/game-auth/v1/"
USER_AGENT = "WibsverseLauncher/0.4"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class WibsVerseAuthError(RuntimeError):
    """A safe-to-display WibsVerse authentication error."""


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.csrf = ""
        self.forms: list[dict] = []
        self._form: dict | None = None
        self._text: list[str] = []
        self._strong_depth = 0
        self.strong_text: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("name", "").lower() == "csrf-token":
            self.csrf = values.get("content", "")
        elif tag == "form":
            self._form = {"action": values.get("action", ""), "method": values.get("method", "get").lower(), "inputs": {}}
        elif tag == "input" and self._form is not None:
            name = values.get("name")
            if name:
                self._form["inputs"][name] = values.get("value", "")
        elif tag == "strong":
            self._strong_depth += 1

    def handle_endtag(self, tag: str):
        if tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None
        elif tag == "strong" and self._strong_depth:
            self._strong_depth -= 1

    def handle_data(self, data: str):
        self._text.append(data)
        if self._strong_depth:
            self.strong_text.append(data)

    @property
    def text(self) -> str:
        return " ".join(" ".join(self._text).split())


class _PrimaryMinecraftProfileParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading = ""
        self._heading_depth = 0
        self._cell: str | None = None
        self._cell_text = ""
        self._row_label = ""
        self.username = ""

    def handle_starttag(self, tag: str, attrs):
        if tag == "h2":
            self._heading_depth = 1
            self.heading = ""
        elif tag in {"th", "td"}:
            self._cell = tag
            self._cell_text = ""

    def handle_endtag(self, tag: str):
        if tag == "h2":
            self._heading_depth = 0
        elif tag == self._cell and self._cell:
            value = " ".join(self._cell_text.split())
            if self.heading.strip().casefold() == "minecraft profile":
                if tag == "th":
                    self._row_label = value.casefold()
                elif self._row_label in {"username", "minecraft username", "profile name"}:
                    self.username = value
            self._cell = None

    def handle_data(self, data: str):
        if self._heading_depth:
            self.heading += data
        if self._cell:
            self._cell_text += data


class _SameSiteRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlparse(newurl)
        if target.scheme != "https" or target.hostname != "www.wibsverse.com":
            raise WibsVerseAuthError("WibsVerse redirected the login to an unexpected site.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class LinkCode:
    code: str
    expires_at: datetime


@dataclass(frozen=True)
class GameAuthCredential:
    ott: str
    runtime_uuid: str
    minecraft_username: str
    proof_signature: str
    expires_at_epoch_seconds: int


class WibsVerseWebSession:
    """A short-lived, in-memory website session used to request a link code."""

    def __init__(self, opener=None, timeout: int = 20):
        cookies = http.cookiejar.CookieJar()
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookies), _SameSiteRedirects()
        )
        self.timeout = timeout

    def _request(self, url: str, data: bytes | None = None) -> tuple[str, str]:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "www.wibsverse.com":
            raise WibsVerseAuthError("Refusing an unexpected WibsVerse URL.")
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = SITE_ORIGIN
            headers["Referer"] = SITE_ORIGIN + "/account"
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                final = urllib.parse.urlparse(response.geturl())
                if final.scheme != "https" or final.hostname != "www.wibsverse.com":
                    raise WibsVerseAuthError("WibsVerse redirected the login to an unexpected site.")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except WibsVerseAuthError:
            raise
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise WibsVerseAuthError("Could not reach WibsVerse. Check your internet connection and try again.") from error
        if len(body) > MAX_RESPONSE_BYTES:
            raise WibsVerseAuthError("WibsVerse returned an unexpectedly large page.")
        return response.geturl(), body.decode("utf-8", errors="replace")

    @staticmethod
    def _parse(html: str) -> _PageParser:
        page = _PageParser()
        page.feed(html)
        return page

    @staticmethod
    def linked_minecraft_username(html: str) -> str | None:
        if "No verified Minecraft profile" in html:
            return None
        parser = _PrimaryMinecraftProfileParser()
        parser.feed(html)
        return parser.username or None

    @staticmethod
    def can_link_minecraft_profile(html: str) -> bool:
        page = _PageParser()
        page.feed(html)
        return any(
            form["method"] == "post" and "/account/minecraft/link-code" in form["action"]
            for form in page.forms
        )

    def login(self, email: str, password: str) -> str:
        if not email.strip() or not password:
            raise WibsVerseAuthError("Enter your WibsVerse email and password.")
        _url, html = self._request(SITE_ORIGIN + "/login")
        page = self._parse(html)
        form = next((form for form in page.forms if form["method"] == "post" and "/login" in form["action"]), None)
        token = page.csrf or (form["inputs"].get("_token", "") if form else "")
        if not form or not token:
            raise WibsVerseAuthError("Could not read the current WibsVerse login form.")
        values = dict(form["inputs"])
        values.update({"_token": token, "login": email.strip(), "password": password, "remember": "1"})
        url = urllib.parse.urljoin(SITE_ORIGIN + "/", form["action"])
        final_url, result = self._request(url, urllib.parse.urlencode(values).encode("utf-8"))
        if urllib.parse.urlparse(final_url).path.rstrip("/") == "/email/verify":
            raise WibsVerseAuthError("Verify your WibsVerse email address before linking Minecraft.")
        final_url, account_html = self._request(SITE_ORIGIN + "/account")
        parsed = self._parse(account_html)
        if urllib.parse.urlparse(final_url).path.rstrip("/") != "/account" or not any(
            "/logout" in form["action"] for form in parsed.forms
        ):
            text = self._parse(result).text.lower()
            if "invalid" in text or "incorrect" in text:
                raise WibsVerseAuthError("WibsVerse rejected that email or password.")
            raise WibsVerseAuthError("WibsVerse sign-in did not complete. Check your credentials and account status.")
        return account_html

    def generate_link_code(self, password: str) -> LinkCode:
        # Re-authenticate the protected action using a fresh, session-specific CSRF token.
        confirm_url, confirm_html = self._request(SITE_ORIGIN + "/user/confirm-password")
        page = self._parse(confirm_html)
        form = next((form for form in page.forms if "/user/confirm-password" in form["action"]), None)
        token = page.csrf or (form["inputs"].get("_token", "") if form else "")
        if form and token:
            values = dict(form["inputs"])
            values.update({"_token": token, "password": password})
            self._request(
                urllib.parse.urljoin(SITE_ORIGIN + "/", form["action"]),
                urllib.parse.urlencode(values).encode("utf-8"),
            )
        elif urllib.parse.urlparse(confirm_url).path.rstrip("/") != "/account":
            raise WibsVerseAuthError("Could not read WibsVerse's password-confirmation form.")

        _url, account_html = self._request(SITE_ORIGIN + "/account")
        account = self._parse(account_html)
        code_form = next(
            (item for item in account.forms if "/account/minecraft/link-code" in item["action"] and item["method"] == "post"),
            None,
        )
        code_token = account.csrf or (code_form["inputs"].get("_token", "") if code_form else "")
        if not code_form or not code_token:
            raise WibsVerseAuthError("Could not find the WibsVerse Minecraft link-code form.")
        values = dict(code_form["inputs"])
        values["_token"] = code_token
        _url, result = self._request(
            urllib.parse.urljoin(SITE_ORIGIN + "/", code_form["action"]),
            urllib.parse.urlencode(values).encode("utf-8"),
        )
        text = self._parse(result).text
        code_match = re.search(r"\b([A-Z0-9]{4}(?:-[A-Z0-9]{4}){3})\b", text)
        expiry_match = re.search(r"Expires at\s+(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d))", text)
        if not code_match or not expiry_match:
            if "/email/verify" in _url or "verify your email" in text.lower():
                raise WibsVerseAuthError("Verify your WibsVerse email address before linking Minecraft.")
            raise WibsVerseAuthError("WibsVerse did not return a Minecraft link code.")
        try:
            expires = datetime.fromisoformat(expiry_match.group(1).replace("Z", "+00:00"))
        except ValueError as error:
            raise WibsVerseAuthError("WibsVerse returned an invalid link-code expiry.") from error
        return LinkCode(code_match.group(1), expires.astimezone(timezone.utc))


class WibsVerseGameAuthClient:
    """Implements the challenge/signature protocol used by WibsVerse SSO 1.0.0."""

    def __init__(self, opener=None, timeout: int = 15):
        self.opener = opener or urllib.request.build_opener(_NoRedirects())
        self.timeout = timeout

    def _post_json(self, endpoint: str, body: dict) -> dict:
        url = GAME_AUTH_ORIGIN + endpoint
        request = urllib.request.Request(
            url,
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.geturl() != url:
                    raise WibsVerseAuthError("The WibsVerse game-auth service redirected unexpectedly.")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
        except WibsVerseAuthError:
            raise
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise WibsVerseAuthError("Too many WibsVerse login attempts. Wait a moment and try again.") from error
            if error.code in {401, 403}:
                raise WibsVerseAuthError("WibsVerse rejected the account password or Minecraft link.") from error
            raise WibsVerseAuthError("WibsVerse game authentication is temporarily unavailable.") from error
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise WibsVerseAuthError("Could not reach WibsVerse game authentication.") from error
        if len(payload) > MAX_RESPONSE_BYTES or status != 200:
            raise WibsVerseAuthError("WibsVerse returned an invalid game-auth response.")
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WibsVerseAuthError("WibsVerse returned an invalid game-auth response.") from error
        if not isinstance(data, dict):
            raise WibsVerseAuthError("WibsVerse returned an invalid game-auth response.")
        return data

    def authenticate(self, minecraft_username: str, password: str) -> GameAuthCredential:
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", minecraft_username):
            raise WibsVerseAuthError("The offline Minecraft username is invalid.")
        if not password or len(password) > 128:
            raise WibsVerseAuthError("Enter the WibsVerse password for this account.")
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        except ImportError as error:
            raise WibsVerseAuthError("The launcher is missing its secure authentication component.") from error

        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = private_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        encoded_public_key = base64.urlsafe_b64encode(public_key).decode("ascii").rstrip("=")
        challenge = self._post_json("challenges", {
            "minecraft_username": minecraft_username,
            "ephemeral_public_key": encoded_public_key,
        })
        if set(challenge) != {"challenge_token", "expires_at"}:
            raise WibsVerseAuthError("WibsVerse returned an invalid login challenge.")
        challenge_token = challenge.get("challenge_token")
        if not isinstance(challenge_token, str) or not re.fullmatch(r"[0-9a-f]{64}", challenge_token):
            raise WibsVerseAuthError("WibsVerse returned an invalid login challenge.")
        try:
            challenge_expiry = datetime.fromisoformat(str(challenge["expires_at"]).replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise WibsVerseAuthError("WibsVerse returned an invalid login challenge.") from error
        if challenge_expiry <= datetime.now(timezone.utc):
            raise WibsVerseAuthError("The WibsVerse login challenge expired. Try again.")

        issued = self._post_json("password", {"challenge_token": challenge_token, "password": password})
        if set(issued) != {"ott", "runtime_uuid", "minecraft_username", "expires_at"}:
            raise WibsVerseAuthError("WibsVerse returned an invalid launcher credential.")
        ott = issued.get("ott")
        runtime_uuid = issued.get("runtime_uuid")
        issued_username = issued.get("minecraft_username")
        if not isinstance(ott, str) or not re.fullmatch(r"[0-9a-f]{64}", ott):
            raise WibsVerseAuthError("WibsVerse returned an invalid launcher credential.")
        if not isinstance(runtime_uuid, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", runtime_uuid
        ):
            raise WibsVerseAuthError("WibsVerse returned an invalid launcher credential.")
        if issued_username != minecraft_username:
            raise WibsVerseAuthError("The WibsVerse credential is for a different Minecraft username.")
        try:
            expires = datetime.fromisoformat(str(issued["expires_at"]).replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise WibsVerseAuthError("WibsVerse returned an invalid launcher credential.") from error
        if expires <= datetime.now(timezone.utc):
            raise WibsVerseAuthError("The WibsVerse launcher credential expired. Try again.")

        payload = f"WVLAUNCH1\n{minecraft_username}\n{runtime_uuid}\n{ott}".encode("utf-8")
        der_signature = private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
        r_value, s_value = decode_dss_signature(der_signature)
        raw_signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
        signature = base64.urlsafe_b64encode(raw_signature).decode("ascii").rstrip("=")
        return GameAuthCredential(
            ott=ott,
            runtime_uuid=runtime_uuid,
            minecraft_username=issued_username,
            proof_signature=signature,
            expires_at_epoch_seconds=int(expires.timestamp()),
        )


def save_password(account_key: str, password: str) -> None:
    """Persist a WibsVerse password using the platform credential-vault backend."""
    try:
        import keyring
        keyring.set_password("WibsverseLauncher", account_key, password)
    except Exception as error:
        raise WibsVerseAuthError("Could not save the WibsVerse password in the system credential vault.") from error


def load_password(account_key: str) -> str | None:
    try:
        import keyring
        return keyring.get_password("WibsverseLauncher", account_key)
    except Exception as error:
        raise WibsVerseAuthError("Could not read the WibsVerse password from the system credential vault.") from error


def delete_password(account_key: str) -> None:
    try:
        import keyring
        keyring.delete_password("WibsverseLauncher", account_key)
    except Exception:
        # Deleting an absent credential is an idempotent account cleanup operation.
        pass
