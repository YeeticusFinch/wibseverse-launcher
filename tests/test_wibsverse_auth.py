import base64
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wibsverse_auth import (
    WibsVerseGameAuthClient, WibsVerseWebSession, delete_password, load_password, save_password,
)


class Response:
    def __init__(self, body: str | dict, url: str):
        self.body = json.dumps(body).encode() if isinstance(body, dict) else body.encode()
        self.url = url
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, _limit=-1):
        return self.body

    def geturl(self):
        return self.url


class QueueOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return self.responses.pop(0)


class WibsVerseWebTests(unittest.TestCase):
    def test_primary_minecraft_profile_detection(self):
        unlinked = '<h2>Minecraft Profile</h2><h2>No verified Minecraft profile</h2>'
        linked = '''<section><h2>Minecraft Profile</h2><table><tr><th>Username</th>
            <td>Player_123</td></tr></table></section>'''
        self.assertIsNone(WibsVerseWebSession.linked_minecraft_username(unlinked))
        self.assertEqual(WibsVerseWebSession.linked_minecraft_username(linked), "Player_123")
        code_form = '<form method="post" action="https://www.wibsverse.com/account/minecraft/link-code"></form>'
        self.assertTrue(WibsVerseWebSession.can_link_minecraft_profile(code_form))
        self.assertFalse(WibsVerseWebSession.can_link_minecraft_profile(linked))

    def test_login_and_link_code_use_fresh_csrf_tokens(self):
        login_html = '''<meta name="csrf-token" content="login-csrf">
            <form method="POST" action="https://www.wibsverse.com/login">
            <input name="_token" value="login-csrf"></form>'''
        account_html = '''<meta name="csrf-token" content="account-csrf">
            <form method="POST" action="https://www.wibsverse.com/logout"></form>
            <form method="post" action="https://www.wibsverse.com/account/minecraft/link-code">
            <input name="_token" value="account-csrf"></form>'''
        confirm_html = '''<meta name="csrf-token" content="confirm-csrf">
            <form method="POST" action="https://www.wibsverse.com/user/confirm-password">
            <input name="_token" value="confirm-csrf"></form>'''
        result_html = '''<div>Your single-use Minecraft link code
            <strong>ABCD-1234-EFGH-5678</strong>
            Expires at 2026-11-11T14:38:24+00:00.</div>'''
        opener = QueueOpener([
            Response(login_html, "https://www.wibsverse.com/login"),
            Response("signed in", "https://www.wibsverse.com/account"),
            Response(account_html, "https://www.wibsverse.com/account"),
            Response(confirm_html, "https://www.wibsverse.com/user/confirm-password"),
            Response("confirmed", "https://www.wibsverse.com/account"),
            Response(account_html, "https://www.wibsverse.com/account"),
            Response(result_html, "https://www.wibsverse.com/account#minecraft-identities"),
        ])
        session = WibsVerseWebSession(opener=opener)
        session.login("player@example.com", "website-password")
        code = session.generate_link_code("website-password")

        self.assertEqual(code.code, "ABCD-1234-EFGH-5678")
        self.assertEqual(len(opener.requests), 7)
        login_form = parse_qs(opener.requests[1].data.decode())
        self.assertEqual(login_form["_token"], ["login-csrf"])
        self.assertEqual(login_form["login"], ["player@example.com"])
        confirm_form = parse_qs(opener.requests[4].data.decode())
        self.assertEqual(confirm_form["_token"], ["confirm-csrf"])
        link_form = parse_qs(opener.requests[6].data.decode())
        self.assertEqual(link_form["_token"], ["account-csrf"])
        self.assertEqual(urlparse(opener.requests[6].full_url).path, "/account/minecraft/link-code")

    def test_password_vault_uses_keyring_api_not_launcher_settings(self):
        stored = {}
        fake = SimpleNamespace(
            set_password=lambda service, user, value: stored.__setitem__((service, user), value),
            get_password=lambda service, user: stored.get((service, user)),
            delete_password=lambda service, user: stored.pop((service, user)),
        )
        with patch.dict(sys.modules, {"keyring": fake}):
            save_password("offline:uuid", "never-in-settings")
            self.assertEqual(load_password("offline:uuid"), "never-in-settings")
            delete_password("offline:uuid")
        self.assertEqual(stored, {})


try:
    import cryptography  # noqa: F401
except ImportError:
    CRYPTO_AVAILABLE = False
else:
    CRYPTO_AVAILABLE = True


@unittest.skipUnless(CRYPTO_AVAILABLE, "cryptography dependency is installed by requirements.txt")
class WibsVerseGameAuthTests(unittest.TestCase):
    def test_game_auth_uses_ephemeral_p256_key_and_returns_p1363_proof(self):
        runtime_uuid = "01234567-89ab-4cde-8fab-0123456789ab"
        opener = QueueOpener([
            Response({"challenge_token": "a" * 64, "expires_at": "2099-01-01T00:00:00+00:00"},
                     "https://wibsverse.com/api/game-auth/v1/challenges"),
            Response({"ott": "b" * 64, "runtime_uuid": runtime_uuid,
                      "minecraft_username": "Player_123", "expires_at": "2099-01-01T00:00:00+00:00"},
                     "https://wibsverse.com/api/game-auth/v1/password"),
        ])
        credential = WibsVerseGameAuthClient(opener=opener).authenticate("Player_123", "website-password")
        challenge_body = json.loads(opener.requests[0].data)
        password_body = json.loads(opener.requests[1].data)
        self.assertEqual(challenge_body["minecraft_username"], "Player_123")
        self.assertTrue(challenge_body["ephemeral_public_key"])
        self.assertEqual(password_body, {"challenge_token": "a" * 64, "password": "website-password"})
        self.assertEqual(credential.ott, "b" * 64)
        self.assertEqual(credential.runtime_uuid, runtime_uuid)
        self.assertEqual(len(base64.urlsafe_b64decode(credential.proof_signature + "==")), 64)


if __name__ == "__main__":
    unittest.main()
