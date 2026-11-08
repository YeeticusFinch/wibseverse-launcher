import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
from skinutils import texture_url_from_profile, skin_png_for_uuid


class IntegrationHelpersTests(unittest.TestCase):
    def test_standard_prism_path_on_windows(self):
        with patch.dict(os.environ, {"APPDATA": r"C:\Users\Player\AppData\Roaming"}):
            self.assertEqual(str(core.standard_prism_data_dir()),
                             str(Path(r"C:\Users\Player\AppData\Roaming") / "PrismLauncher"))

    def test_prism_uuid_is_public_metadata_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "accounts.json").write_text(json.dumps({"accounts": [
                {"type": "MSA", "profile": {"name": "PlayerA", "id": "853c80ef-3c37-49fd-aa49-938b674adae6"},
                 "accessToken": "SECRET_NEVER_COPIED"},
                {"type": "Offline", "profile": {"name": "OfflinePlayer"}}
            ]}))
            result = core.prism_accounts(root)
            self.assertEqual(result[0]["uuid"], "853c80ef3c3749fdaa49938b674adae6")
            self.assertNotIn("SECRET_NEVER_COPIED", str(result))
            self.assertEqual(result[1]["type"], "Offline")

    def test_migrates_modpack_and_world_without_touching_original_or_other_instances(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            old = base / "legacy"
            new = base / "standard"
            source = core.game_dir(old)
            (source / "saves" / "MyWorld").mkdir(parents=True)
            (source / "mods").mkdir()
            (source / "mods" / "pack.jar").write_bytes(b"pack")
            (source / "saves" / "MyWorld" / "level.dat").write_bytes(b"world")
            (old / "accounts.json").write_text("DO NOT COPY")
            other = new / "instances" / "my-other-instance" / "something"
            other.mkdir(parents=True)
            (other / "myfile").write_text("keep")
            self.assertTrue(core.migrate_legacy_instance(old, new))
            self.assertEqual((core.game_dir(new) / "mods" / "pack.jar").read_bytes(), b"pack")
            self.assertEqual((core.game_dir(new) / "saves" / "MyWorld" / "level.dat").read_bytes(), b"world")
            self.assertFalse((new / "accounts.json").exists())
            self.assertEqual((other / "myfile").read_text(), "keep")
            self.assertEqual((source / "saves" / "MyWorld" / "level.dat").read_bytes(), b"world")
            with self.assertRaises(core.LauncherError):
                core.migrate_legacy_instance(old, new)

    def test_skin_url_validation_and_https_upgrade(self):
        def prop(url):
            payload = {"textures": {"SKIN": {"url": url}}}
            token = base64.b64encode(json.dumps(payload).encode()).decode()
            return {"properties": [{"name": "textures", "value": token}]}
        hash_text = "a" * 64
        result = texture_url_from_profile(prop("http://textures.minecraft.net/texture/" + hash_text))
        self.assertEqual(result, "https://textures.minecraft.net/texture/" + hash_text)
        self.assertIsNone(texture_url_from_profile(prop("https://example.net/texture/" + hash_text)))
        self.assertIsNone(texture_url_from_profile(prop("https://textures.minecraft.net/texture/../../oops")))
        self.assertIsNone(texture_url_from_profile({"properties": [{"name": "textures", "value": "???"}]}))
        self.assertIsNone(skin_png_for_uuid("INVALID", Path("/no/such/cache")))


if __name__ == "__main__":
    unittest.main()
