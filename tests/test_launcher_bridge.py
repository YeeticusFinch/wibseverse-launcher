import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher
import core


class LauncherBridgeTests(unittest.TestCase):
    def test_installs_bundled_link_bridge_into_instance_mods(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            bundled = base / "bundle" / "mods" / launcher.LINK_BRIDGE_JAR
            bundled.parent.mkdir(parents=True)
            bundled.write_bytes(b"bridge-jar-test")
            root = base / "instance"
            with patch.object(launcher.sys, "_MEIPASS", str(base / "bundle"), create=True):
                installed = launcher.install_launcher_link_bridge(root)
            self.assertEqual(installed, core.game_dir(root) / "mods" / launcher.LINK_BRIDGE_JAR)
            self.assertEqual(installed.read_bytes(), b"bridge-jar-test")
            self.assertFalse(installed.with_name(installed.name + ".installtmp").exists())


if __name__ == "__main__":
    unittest.main()
