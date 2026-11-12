import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core

URL = "https://cdn.wibsverse.com/modpacks/ds/manual/20261029-2.3.0/WibsDemonSlayer-2.3.0.zip"


class LauncherCoreTests(unittest.TestCase):
    def test_macos_platform_release_and_java_metadata(self):
        with patch.object(core.sys, "platform", "darwin"), patch("platform.machine", return_value="arm64"):
            asset, checksum, url = core.hmc_release()
            self.assertEqual(asset, "headlessmc-launcher-macos-arm64")
            self.assertRegex(checksum, r"^[0-9a-f]{64}$")
            self.assertIn("headlessmc-launcher-macos-arm64", url)
            self.assertIn("architecture=aarch64", core.java_metadata_url())
            self.assertIn("os=mac", core.java_metadata_url())
            self.assertEqual(core.headlessmc_executable_path(Path("/tmp/hmc")).name, "headlessmc")

    def test_extract_java_tarball(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            archive = base / "java.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                content = b"#!/bin/sh\necho 'openjdk version \"17.0.1\"'\n"
                info = tarfile.TarInfo("jdk/Contents/Home/bin/java")
                info.size = len(content)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(content))
            destination = base / "managed-java"
            result = core._extract_java_archive(archive, destination)
            self.assertEqual(result, destination)
            java = destination / "bin" / "java"
            self.assertTrue(java.is_file())
            if os.name != "nt":
                self.assertTrue(java.stat().st_mode & 0o100)

    def test_latest_launcher_release_uses_newest_non_draft_tag(self):
        payload = json.dumps([
            {"tag_name": "0.3", "draft": False, "html_url": "https://github.com/YeeticusFinch/wibseverse-launcher/releases/tag/0.3", "assets": [
                {"name": "WibsverseLauncherAutoInstall.exe", "browser_download_url": "https://github.com/YeeticusFinch/wibseverse-launcher/releases/download/0.3/WibsverseLauncherAutoInstall.exe", "digest": "sha256:" + "a" * 64},
            ]},
            {"tag_name": "0.9", "draft": True, "html_url": "https://github.com/YeeticusFinch/wibseverse-launcher/releases/tag/0.9", "assets": []},
            {"tag_name": "0.2", "draft": False, "html_url": "https://github.com/YeeticusFinch/wibseverse-launcher/releases/tag/0.2", "assets": []},
        ]).encode()

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        with patch.object(core.urllib.request, "urlopen", return_value=Response(payload)):
            release = core.fetch_latest_launcher_release()
        self.assertIsNotNone(release)
        assert release is not None
        self.assertEqual(release.version, "0.3")
        self.assertEqual(release.asset_name, "WibsverseLauncherAutoInstall.exe")
        self.assertEqual(release.asset_sha256, "a" * 64)

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
            root = base / "HeadlessMc"
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
            state = json.loads((root / "wibsverse-installed.json").read_text())
            self.assertEqual(state["version"], "2.3.1")
            config = (root / "config" / "headlessmc" / "config.properties").read_text()
            self.assertIn("hmc.files.game=", config)
            self.assertIn("hmc.files.game-for-each-version=false", config)
            self.assertIn("hmc.java.download=false", config)
            self.assertNotIn("hmc.java.check-for-updates", config)
            self.assertNotIn("hmc.launcher.auto-download-specifics", config)

    def test_hmc_commands_pin_forge_and_launch_headlessly(self):
        root = Path("C:/Wibsverse/HeadlessMc")
        self.assertEqual(core.hmc_login_command("hmc"), ["hmc", "account", "--provider", "default", "login"])
        self.assertEqual(core.hmc_account_list_command("hmc"), ["hmc", "account", "list"])
        self.assertEqual(core.hmc_offline_login_command("hmc"), ["hmc", "account", "--provider", "offline", "login"])
        self.assertEqual(core.hmc_select_account_command("hmc", "Player_123")[-2:], ["select", "Player_123"])
        self.assertEqual(core.hmc_remove_account_command("hmc", "Player_123")[-2:], ["remove", "Player_123"])
        self.assertEqual(
            core.hmc_profile_command("hmc", root),
            ["hmc", "profile", "add", "forge", "1.20.1", "47.4.23", "--force", "--name", "wibsverse", "--dir", str(core.game_dir(root))],
        )
        command = core.hmc_launch_command("hmc", "Player_123")
        self.assertEqual(command[:2], ["hmc", "launch"])
        self.assertNotIn("--headless", command)
        self.assertIn("--server", command)
        self.assertEqual(command[-1], "wibsverse")

    def test_hmc_account_table_exposes_public_microsoft_metadata_only(self):
        output = (
            "name         uuid                                  type  provider\n"
            "Player_123   853c80ef-3c37-49fd-aa49-938b674adae6  msa   default\n"
            "Steve        00000000000000000000000000000000      offline offline\n"
        )
        accounts = core.parse_hmc_accounts(output)
        self.assertEqual(accounts[0]["name"], "Player_123")
        self.assertEqual(accounts[0]["uuid"], "853c80ef3c3749fdaa49938b674adae6")
        offline = core.parse_hmc_accounts(
            "\x1b[1mOfflineUser\x1b[0m  146cd464-990f-45e9-a94c-706442ec758c  msa  offline\n"
        )
        self.assertEqual(offline[0]["mode"], "offline")
        self.assertEqual(len(accounts), 1)

    def test_hmc_profile_pin_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = core.hmc_profile_path(root)
            profile.parent.mkdir(parents=True)
            profile.write_text(json.dumps({"currentVersion": {
                "platform": "forge", "version": "1.20.1", "build": "47.4.23"
            }}))
            self.assertTrue(core.hmc_profile_is_pinned(root))
            profile.write_text(json.dumps({"currentVersion": {
                "platform": "forge", "version": "1.20.1", "build": "47.4.24"
            }}))
            self.assertFalse(core.hmc_profile_is_pinned(root))

    def test_create_hmc_offline_account_writes_tokenless_record(self):
        self.assertEqual(core.offline_player_uuid("Notch"), "b50ad385-829d-3141-a216-7e7d7539ba7f")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            account = core.create_hmc_offline_account(root, "LocalPlayer")
            self.assertEqual(account["provider"], "offline")
            self.assertEqual(account["token"], "")
            stored = json.loads(core.hmc_offline_accounts_path(root).read_text())
            self.assertEqual(stored["version"], 0)
            self.assertEqual(stored["accounts"]["LocalPlayer"]["name"], "LocalPlayer")
            with self.assertRaises(core.LauncherError):
                core.create_hmc_offline_account(root, "localplayer")

    def test_account_credential_key_uses_stable_uuid_and_offline_rollback(self):
        account = {"mode": "offline", "uuid": "01234567-89ab-4cde-8fab-0123456789ab"}
        self.assertEqual(core.account_credential_key(account), "offline:0123456789ab4cde8fab0123456789ab")
        with self.assertRaises(core.LauncherError):
            core.account_credential_key({"mode": "offline", "uuid": "invalid"})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            created = core.create_hmc_offline_account(root, "RollbackPlayer")
            self.assertTrue(core.remove_hmc_offline_account(root, created["name"]))
            self.assertFalse(core.remove_hmc_offline_account(root, created["name"]))
            self.assertEqual(json.loads(core.hmc_offline_accounts_path(root).read_text())["accounts"], {})

    def test_install_headlessmc_verifies_and_replaces_binary_atomically(self):
        self.assertRegex(core.HMC_SHA256, r"^[0-9a-f]{64}$")
        payload = b"fake-headlessmc-binary"

        class Response(io.BytesIO):
            headers = {"Content-Length": str(len(payload))}

            def geturl(self):
                return core.HMC_DOWNLOAD_URL

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(core.urllib.request, "urlopen", return_value=Response(payload)):
                with patch.object(core, "HMC_SHA256", hashlib.sha256(payload).hexdigest()):
                    executable = core.install_headlessmc(root)
            self.assertEqual(executable, core.headlessmc_executable_path(root))
            self.assertEqual(executable.read_bytes(), payload)
            self.assertFalse(executable.with_name("headlessmc.exe.download").exists())


if __name__ == "__main__":
    unittest.main()
