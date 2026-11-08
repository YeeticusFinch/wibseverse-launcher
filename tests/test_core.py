import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core

URL = "https://cdn.wibsverse.com/modpacks/ds/manual/20261029-2.3.0/WibsDemonSlayer-2.3.0.zip"


class LauncherCoreTests(unittest.TestCase):
    def test_official_download_parser(self):
        html = f'<a href="{URL}">Download Mods ZIP</a>'
        release = core.parse_official_download_page(html)
        self.assertEqual(release.version, "2.3.0")
        self.assertEqual(release.url, URL)

    def test_url_validation(self):
        with self.assertRaises(core.LauncherError):
            core.Release("2.3.0", "http://cdn.wibsverse.com/p.zip").validate()
        with self.assertRaises(core.LauncherError):
            core.Release("2.3.0", "https://evil.example/p.zip").validate()

    def test_safe_zip_paths(self):
        self.assertEqual(core._safe_normalize("a/.minecraft/mods/test.jar"), "mods/test.jar")
        self.assertEqual(core._safe_normalize("config/main.json"), "config/main.json")
        self.assertEqual(core._safe_normalize("mod.jar"), "mods/mod.jar")
        self.assertIsNone(core._safe_normalize("saves/world/level.dat"))
        for path in ("../foo/mods/evil.jar", "/mods/evil.jar", "C:/mods/evil.jar"):
            with self.assertRaises(core.LauncherError):
                core._safe_normalize(path)

    def test_install_and_update_does_not_delete_save(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "PrismData"
            os.environ["LOCALAPPDATA"] = str(base)
            z1 = base / "pack1.zip"
            z2 = base / "pack2.zip"
            with zipfile.ZipFile(z1, "w") as z:
                z.writestr("WibDS/mods/old.jar", b"old mod")
                z.writestr("WibDS/config/a.cfg", b"version1")
                z.writestr("WibDS/saves/world/level.dat", b"bad should ignore")
            with zipfile.ZipFile(z2, "w") as z:
                z.writestr("mods/new.jar", b"new mod")
                z.writestr("config/a.cfg", b"version2")
            counter = [0]

            def fake_download(release, dest, progress):
                import shutil
                file = (z1, z2)[counter[0]]
                counter[0] += 1
                shutil.copy2(file, dest)
                return hashlib.sha256(file.read_bytes()).hexdigest()

            r1 = core.Release("2.3.0", URL)
            r2 = core.Release("2.3.1", URL.replace("2.3.0", "2.3.1"))
            with patch.object(core, "_download_archive", fake_download):
                state1 = core.install_release(root, r1)
                self.assertEqual(state1["version"], "2.3.0")
                game = core.game_dir(root)
                (game / "saves" / "myworld").mkdir(parents=True)
                (game / "saves" / "myworld" / "level.dat").write_bytes(b"my save")
                (game / "mods" / "personal.jar").write_bytes(b"my mod")
                state2 = core.install_release(root, r2)
            self.assertEqual(state2["version"], "2.3.1")
            self.assertFalse((game / "mods" / "old.jar").exists())
            self.assertTrue((game / "mods" / "new.jar").exists())
            self.assertTrue((game / "mods" / "personal.jar").exists())
            self.assertEqual((game / "config" / "a.cfg").read_bytes(), b"version2")
            self.assertEqual((game / "saves" / "myworld" / "level.dat").read_bytes(), b"my save")
            mmc = json.loads((core.instance_dir(root) / "mmc-pack.json").read_text())
            self.assertEqual(mmc["components"][1]["version"], "47.4.23")

    def test_accounts_and_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "accounts.json").write_text(json.dumps({"accounts": [
                {"type": "MSA", "profile": {"name": "Player_123"}, "accessToken": "never_show"}
            ]}))
            accounts = core.prism_accounts(root)
            self.assertEqual(accounts, [{"name": "Player_123", "type": "MSA"}])
            cmd = core.prism_launch_command("prism", root, online_name="Player_123")
            self.assertIn("--profile", cmd)
            self.assertIn("play.wibsverse.com", cmd)
            self.assertNotIn("never_show", str(accounts))


if __name__ == "__main__":
    unittest.main()
