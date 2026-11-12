import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import client_mods
from core import LauncherError, game_dir


def jar_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
    return buffer.getvalue()


class ClientModsTests(unittest.TestCase):
    def test_inventory_excludes_pack_jars_in_enabled_and_disabled_forms(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mods = game_dir(root) / "mods"
            mods.mkdir(parents=True)
            (mods / "pack-mod.jar.disabled").write_bytes(b"pack")
            (mods / "custom.jar").write_bytes(b"custom")
            (mods / "manual.jar.disabled").write_bytes(b"manual")
            (mods / "readme.txt").write_text("ignore")
            installed = {"files": {"mods/pack-mod.jar": "a" * 64}}

            result = client_mods.list_custom_mods(root, installed)

            self.assertEqual([mod.filename for mod in result], ["custom.jar", "manual.jar"])
            self.assertEqual([mod.enabled for mod in result], [True, False])

    def test_enable_disable_and_delete_extra_mod(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            mods = game_dir(root) / "mods"
            mods.mkdir(parents=True)
            source = mods / "extra.jar"
            source.write_bytes(b"jar")
            mod = client_mods.ClientMod(source, True)

            disabled = client_mods.set_mod_enabled(root, mod, False)
            self.assertEqual(disabled.path.name, "extra.jar.disabled")
            self.assertFalse(disabled.enabled)
            enabled = client_mods.set_mod_enabled(root, disabled, True)
            self.assertEqual(enabled.path.name, "extra.jar")
            self.assertTrue(enabled.enabled)
            client_mods.delete_mod(root, enabled)
            self.assertFalse(source.exists())

    def test_install_modrinth_version_records_project_and_file_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            content = jar_bytes()
            digest = hashlib.sha1(content).hexdigest()

            class Response(io.BytesIO):
                headers = {"Content-Length": str(len(content))}

                def geturl(self):
                    return "https://cdn.modrinth.com/data/project/versions/version/mod.jar"

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    self.close()

            version = {
                "id": "version-id",
                "project_id": "project-id",
                "version_number": "1.2.3",
                "files": [{
                    "filename": "mod.jar", "url": "https://cdn.modrinth.com/data/project/versions/version/mod.jar",
                    "primary": True, "hashes": {"sha1": digest},
                }],
            }
            with patch.object(client_mods.urllib.request, "urlopen", return_value=Response(content)):
                installed = client_mods.install_modrinth_version(root, {"id": "project-id"}, version)

            self.assertTrue(installed.path.is_file())
            self.assertEqual(installed.sha1, digest)
            state = json.loads(client_mods.mods_state_path(root).read_text(encoding="utf-8"))
            self.assertEqual(state["mods"]["mod.jar"]["version_id"], "version-id")

    def test_bad_checksum_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "mod.jar"
            target.write_bytes(jar_bytes())
            original = target.read_bytes()
            content = jar_bytes()

            class Response(io.BytesIO):
                headers = {"Content-Length": str(len(content))}

                def geturl(self):
                    return "https://cdn.modrinth.com/file.jar"

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    self.close()

            with patch.object(client_mods.urllib.request, "urlopen", return_value=Response(content)):
                with self.assertRaises(LauncherError):
                    client_mods._download_jar("https://cdn.modrinth.com/file.jar", target, "0" * 40)
            self.assertEqual(target.read_bytes(), original)

    def test_search_uses_game_loader_and_mod_facets(self):
        with patch.object(client_mods, "_api_json", return_value={"hits": [{"project_id": "abc"}]}) as request:
            result = client_mods.search_modrinth("map")
        self.assertEqual(result[0]["project_id"], "abc")
        url = request.call_args.args[0]
        self.assertIn("project_type%3Amod", url)
        self.assertIn("versions%3A1.20.1", url)
        self.assertIn("categories%3Aforge", url)


if __name__ == "__main__":
    unittest.main()
