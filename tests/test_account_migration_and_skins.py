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
import skinutils
from skinutils import texture_url_from_profile, skin_png_for_uuid


class IntegrationHelpersTests(unittest.TestCase):
    def test_headlessmc_root_is_isolated_from_normal_installations(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\Player\AppData\Local"}):
            self.assertEqual(str(core.headlessmc_root()),
                             str(Path(r"C:\Users\Player\AppData\Local") / "WibsverseLauncher" / "HeadlessMc"))

    def test_hmc_account_parser_keeps_only_public_microsoft_metadata(self):
        output = "name  uuid                                  type  provider\nPlayerA  853c80ef-3c37-49fd-aa49-938b674adae6  msa   default\n"
        result = core.parse_hmc_accounts(output)
        self.assertEqual(result[0]["uuid"], "853c80ef3c3749fdaa49938b674adae6")
        self.assertNotIn("token", str(result).lower())

    def test_migrates_modpack_and_world_without_touching_original_or_other_instances(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            old = base / "legacy"
            new = base / "standard"
            source = old / "instances" / core.INSTANCE_ID / ".minecraft"
            (source / "saves" / "MyWorld").mkdir(parents=True)
            (source / "mods").mkdir()
            (source / "mods" / "pack.jar").write_bytes(b"pack")
            (source / "saves" / "MyWorld" / "level.dat").write_bytes(b"world")
            (old / "accounts.json").write_text("DO NOT COPY")
            other = new / "unrelated" / "something"
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

    def test_skin_lookup_falls_back_to_rendered_head_when_texture_is_unavailable(self):
        png = b"\x89PNG\r\n\x1a\nrendered-head"
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(skinutils, "_limited_urlopen", side_effect=[OSError("texture expired"), png]) as fetch:
                result = skin_png_for_uuid("853c80ef-3c37-49fd-aa49-938b674adae6", Path(folder))
            self.assertEqual(result, png)
            self.assertEqual(fetch.call_args_list[1].args[1], "mc-heads.net")


if __name__ == "__main__":
    unittest.main()
