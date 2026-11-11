import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sso_handoff import CREDENTIAL_FIELDS, CONTEXT_FIELDS, SsoHandoff, context_digest
from wibsverse_auth import GameAuthCredential


class SsoHandoffTests(unittest.TestCase):
    def test_context_and_credential_follow_installed_mod_field_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            handoff = SsoHandoff.create(
                Path(temporary),
                "01234567-89ab-4cde-8fab-0123456789ab",
                "Player_123",
                "play.wibsverse.com",
                base=Path(temporary),
            )
            try:
                handoff.write_context(4242)
                context = json.loads((handoff.directory / "context.json").read_text())
                self.assertEqual(set(context), CONTEXT_FIELDS)
                self.assertEqual(
                    context["context_sha256"],
                    context_digest(
                        context["session_id"], context["nonce"], context["process_id"],
                        context["runtime_uuid"], context["minecraft_username"], context["target_host"],
                        context["target_port"], context["created_at_epoch_seconds"], context["expires_at_epoch_seconds"],
                    ),
                )
                (handoff.directory / "ready.json").write_text(json.dumps({"session_id": handoff.session_id}))
                handoff.write_credential(GameAuthCredential(
                    "a" * 64,
                    handoff.runtime_uuid,
                    handoff.username,
                    "b" * 86,
                    context["expires_at_epoch_seconds"],
                ))
                credential = json.loads((handoff.directory / "credential.json").read_text())
                self.assertEqual(set(credential), CREDENTIAL_FIELDS)
                self.assertEqual(credential["process_id"], 4242)
                self.assertEqual(credential["handoff_id"], handoff.directory.name)
                handoff.write_link_code(
                    "ABCD-1234-EFGH-5678", datetime.now(timezone.utc) + timedelta(minutes=5)
                )
                link = json.loads((handoff.directory / "link-code.json").read_text())
                self.assertEqual(link["code"], "ABCD-1234-EFGH-5678")
                self.assertEqual(link["minecraft_username"], "Player_123")
            finally:
                handoff.cleanup()


if __name__ == "__main__":
    unittest.main()
