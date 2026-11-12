"""Wibsverse Launcher: custom UI backed by HeadlessMc."""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QThread, QUrl, QRect, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QPainter, QPixmap, QColor, QLinearGradient, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton, QFormLayout,
    QToolButton, QVBoxLayout, QWidget, QCheckBox, QPlainTextEdit, QStackedWidget,
    QInputDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QListWidget, QListWidgetItem, QComboBox,
)

from core import (
    FORGE_VERSION, LAUNCHER_VERSION, MINECRAFT_VERSION, LauncherError, LauncherRelease, Release, app_dir, atomic_json, ensure_instance,
    fetch_latest_release, find_headlessmc, game_dir, hmc_accounts, install_release,
    hmc_environment, hmc_launch_command, hmc_login_command,
    hmc_profile_command, hmc_remove_account_command,
    hmc_profile_is_pinned, hmc_select_account_command, legacy_prism_data_dir,
    account_credential_key, create_hmc_offline_account, remove_hmc_offline_account,
    ensure_java17, install_headlessmc, load_json,
    migrate_legacy_instance, read_installed, run_hmc, fetch_latest_launcher_release,
    download_launcher_asset,
)
from skinutils import skin_png_for_uuid
from wibsverse_auth import (
    GameAuthCredential, WibsVerseAuthError, WibsVerseGameAuthClient,
    WibsVerseWebSession, delete_password, load_password, save_password,
)
from sso_handoff import SsoHandoff, SsoHandoffError
from client_mods import (
    ClientMod, delete_mod, get_project, get_project_dependencies, get_project_versions,
    install_modrinth_version, list_custom_mods, lookup_modrinth_hash, mod_sha1, search_modrinth,
    remember_modrinth_version, set_mod_enabled,
)

PINK = "#D9439F"
INK = "#100B12"
CREAM = "#FFF1DF"
MUTED = "#BAA6B4"
ASSET_DIR = Path(__file__).resolve().parent / "assets"
LINK_BRIDGE_JAR = "wibsverse-launcher-link-bridge-1.0.0.jar"


def launcher_icon_asset() -> Path:
    return ASSET_DIR / "wibsverse_icon.png"


def game_icon_asset() -> Path:
    return ASSET_DIR / "logo.png"


def install_launcher_link_bridge(root: Path) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidates = (
        bundle_root / "mods" / LINK_BRIDGE_JAR,
        Path(__file__).resolve().parent / "launcher-link-bridge" / "build" / "libs" / LINK_BRIDGE_JAR,
    )
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise LauncherError(
            "The WibsVerse link bridge is missing. Build the launcher-link-bridge Forge mod before launching offline accounts."
        )
    destination = game_dir(root) / "mods" / LINK_BRIDGE_JAR
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest():
        return destination
    staged = destination.with_name(destination.name + ".installtmp")
    try:
        shutil.copy2(source, staged)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    return destination


def native_icon_file(source: Path, filename: str) -> Path | None:
    """Create a Windows ICO wrapper around the packaged PNG for native windows."""
    if not source.is_file():
        return None
    target = app_dir() / filename
    try:
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            png = source.read_bytes()
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                return None
            header = struct.pack("<HHH", 0, 1, 1)
            directory = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(header + directory + png)
        return target
    except OSError:
        return None


def game_icon_file() -> Path | None:
    if os.name != "nt":
        return None
    return native_icon_file(game_icon_asset(), "wibsverse_game.ico")


def _window_process_ids(process_id: int) -> set[int]:
    if os.name != "nt":
        return {process_id}

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.windll.kernel32
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return {process_id}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        parents: dict[int, int] = {}
        if kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parents[entry.th32ProcessID] = entry.th32ParentProcessID
                if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        result = {process_id}
        changed = True
        while changed:
            changed = False
            for child, parent in parents.items():
                if parent in result and child not in result:
                    result.add(child)
                    changed = True
        return result
    finally:
        kernel.CloseHandle(snapshot)


def apply_game_window_icon(process_id: int, icon_file: Path) -> bool:
    """Set the packaged Wibsverse icon on Minecraft's native Windows window."""
    if os.name != "nt" or not icon_file.is_file():
        return False
    user32 = ctypes.windll.user32
    process_ids = _window_process_ids(process_id)
    windows: list[int] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_proc
    def collect(hwnd, _param):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in process_ids and user32.IsWindowVisible(hwnd):
            windows.append(hwnd)
        return True

    user32.EnumWindows(collect, 0)
    if not windows:
        return False
    user32.LoadImageW.restype = wintypes.HANDLE
    icon = user32.LoadImageW(None, str(icon_file), 1, 0, 0, 0x00000010 | 0x00000040)
    if not icon:
        return False
    for hwnd in windows:
        user32.SendMessageW(hwnd, 0x0080, 1, icon)
        user32.SendMessageW(hwnd, 0x0080, 0, icon)
        if hasattr(user32, "SetClassLongPtrW"):
            user32.SetClassLongPtrW(hwnd, -14, icon)
            user32.SetClassLongPtrW(hwnd, -34, icon)
    return True


def wait_for_minecraft_process_id(
    game_process_id: int, game_folder: Path, launched_at: float | None = None, timeout: float = 90.0
) -> int:
    """Find the Minecraft Java child so SSO's IPC is bound to the real game PID."""
    try:
        import psutil
    except ImportError as error:
        raise LauncherError("The launcher is missing process inspection support required by WibsVerse SSO.") from error
    target = str(game_folder.resolve()).replace("\\", "/").casefold()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = []
        try:
            root = psutil.Process(game_process_id)
            processes = [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            processes = psutil.process_iter(["pid", "name", "cmdline", "create_time"])
        for process in processes:
            try:
                info = process.info if hasattr(process, "info") else {
                    "pid": process.pid, "name": process.name(), "cmdline": process.cmdline(),
                    "create_time": process.create_time(),
                }
                name = (info.get("name") or "").casefold()
                if name not in {"java.exe", "javaw.exe", "java", "javaw"}:
                    continue
                if launched_at is not None and float(info.get("create_time") or 0) < launched_at - 2:
                    continue
                args = info.get("cmdline") or []
                command = " ".join(args).replace("\\", "/").casefold()
                if target in command or "net.minecraft.client.main.main" in command:
                    matches.append((float(info.get("create_time") or 0), int(info["pid"])))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, TypeError, ValueError):
                continue
        if matches:
            return max(matches)[1]
        time.sleep(0.2)
    raise LauncherError("Could not locate the Minecraft Java process for the WibsVerse SSO handoff.")


class UpdateWorker(QThread):
    progressed = Signal(str, int)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: str, root: Path, release: Release | None = None):
        super().__init__()
        self.operation = operation
        self.root = root
        self.release = release

    def run(self):
        try:
            if self.operation == "check":
                result = fetch_latest_release()
            elif self.operation == "install" and self.release is not None:
                result = install_release(self.root, self.release,
                                         lambda status, value: self.progressed.emit(status, value))
            else:
                raise LauncherError("Invalid update operation")
            self.succeeded.emit(result)
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class LauncherUpdateWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, release: LauncherRelease):
        super().__init__()
        self.release = release

    def run(self):
        try:
            self.succeeded.emit(download_launcher_asset(self.release))
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class LauncherReleaseWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def run(self):
        try:
            self.succeeded.emit(fetch_latest_launcher_release())
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class ModrinthWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, operation: str, *args):
        super().__init__()
        self.operation = operation
        self.args = args

    def run(self):
        try:
            if self.operation == "search":
                result = search_modrinth(self.args[0])
            elif self.operation == "versions":
                project = get_project(self.args[0])
                result = {"project": project, "versions": get_project_versions(self.args[0])}
            elif self.operation == "dependencies":
                result = get_project_dependencies(self.args[0])
            elif self.operation == "install":
                root, project, version, dependencies = self.args[:4]
                replace = self.args[4] if len(self.args) > 4 else None
                installed = []
                for dependency in dependencies:
                    dep_project, dep_version = dependency["project"], dependency["version"]
                    dependency_files = [item for item in dep_version.get("files", []) if isinstance(item, dict)
                                        and str(item.get("filename", "")).lower().endswith(".jar")]
                    primary_file = next((item for item in dependency_files if item.get("primary")),
                                        dependency_files[0] if dependency_files else {})
                    filename = str(primary_file.get("filename") or "")
                    if filename and (
                        (game_dir(root) / "mods" / filename).exists()
                        or (game_dir(root) / "mods" / (filename + ".disabled")).exists()
                    ):
                        continue
                    installed.append(install_modrinth_version(root, dep_project, dep_version))
                installed.append(install_modrinth_version(root, project, version, replace))
                result = installed
            elif self.operation == "update":
                root, mod = self.args
                record = None
                sha1 = mod.sha1 or mod_sha1(mod.path)
                if mod.project_id:
                    project_id = mod.project_id
                else:
                    record = lookup_modrinth_hash(sha1)
                    project_id = str((record or {}).get("project_id") or "")
                if not project_id:
                    result = {"mod": mod, "project": None, "version": None}
                else:
                    project = get_project(project_id)
                    versions = get_project_versions(project_id)
                    if record:
                        remember_modrinth_version(root, mod.filename, record, sha1)
                    latest = versions[0] if versions else None
                    current_id = mod.version_id or str((record or {}).get("id") or "")
                    result = {
                        "root": root, "mod": mod, "project": project, "version": latest,
                        "has_update": bool(latest and current_id and latest.get("id") != current_id),
                        "untracked": not bool(mod.version_id),
                    }
            elif self.operation == "lookup":
                root, mod = self.args
                sha1 = mod.sha1 or mod_sha1(mod.path)
                record = lookup_modrinth_hash(sha1)
                if not record:
                    result = {"mod": mod, "project": None, "version": None}
                else:
                    project = get_project(str(record.get("project_id") or ""))
                    remember_modrinth_version(root, mod.filename, record, sha1)
                    result = {"mod": mod, "project": project, "version": record}
            else:
                raise LauncherError("Unknown Modrinth operation")
            self.succeeded.emit(result)
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class AddModDialog(QDialog):
    requested_install = Signal(object, object)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Add a Mod from Modrinth")
        self.setMinimumSize(620, 520)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.project: dict | None = None
        self.versions: list[dict] = []
        self.worker: ModrinthWorker | None = None
        self.pending_worker_request: tuple[str, tuple] | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(12)
        title = QLabel("Find a Forge mod")
        title.setObjectName("updateTitle")
        layout.addWidget(title)
        hint = QLabel("Showing mods compatible with Minecraft 1.20.1 and Forge.")
        hint.setObjectName("muted")
        layout.addWidget(hint)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search Modrinth")
        self.search.returnPressed.connect(self.search_projects)
        search_row.addWidget(self.search, 1)
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self.search_projects)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)
        self.results = QListWidget()
        self.results.currentRowChanged.connect(self.select_project)
        layout.addWidget(self.results, 1)
        version_row = QHBoxLayout()
        version_row.addWidget(QLabel("Version"))
        self.version = QComboBox()
        version_row.addWidget(self.version, 1)
        layout.addLayout(version_row)
        self.status = QLabel("Search for a mod to begin.")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.install_button = QPushButton("Add Mod")
        self.install_button.setObjectName("primary")
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self.request_selected_install)
        buttons.addWidget(self.install_button)
        cancel = QPushButton("Close")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _run(self, operation: str, *args):
        if self.worker and self.worker.isRunning():
            self.pending_worker_request = (operation, args)
            return
        self.worker = ModrinthWorker(operation, *args)
        self.worker.succeeded.connect(self._worker_succeeded)
        self.worker.failed.connect(self._worker_failed)
        self.worker.finished.connect(self._start_pending_worker)
        self.worker.start()

    def _start_pending_worker(self):
        if self.pending_worker_request:
            operation, args = self.pending_worker_request
            self.pending_worker_request = None
            self._run(operation, *args)

    def search_projects(self):
        query = self.search.text().strip()
        if not query:
            self.status.setText("Enter a search term.")
            return
        self.search_button.setEnabled(False)
        self.status.setText("Searching Modrinth…")
        self._run("search", query)

    def select_project(self, index: int):
        if index < 0 or index >= self.results.count():
            return
        project = self.results.item(index).data(Qt.ItemDataRole.UserRole)
        self.project = project
        self.install_button.setEnabled(False)
        self.status.setText("Loading compatible versions…")
        self._run("versions", project["project_id"])

    def _worker_succeeded(self, result: object):
        self.search_button.setEnabled(True)
        if isinstance(result, list):
            self.results.clear()
            for project in result:
                item = QListWidgetItem(f"{project.get('title', 'Untitled')}  ·  {project.get('author', 'Unknown author')}\n{project.get('description', '')}")
                item.setData(Qt.ItemDataRole.UserRole, project)
                self.results.addItem(item)
            self.status.setText(f"Found {len(result)} compatible mod(s).")
        elif isinstance(result, dict) and "versions" in result:
            self.project = result.get("project") or self.project
            self.versions = result.get("versions", [])
            self.version.clear()
            for version in self.versions:
                self.version.addItem(
                    f"{version.get('version_number', 'Unknown')}  ·  {version.get('version_type', 'release')}",
                    version,
                )
            self.install_button.setEnabled(bool(self.versions))
            self.status.setText("Choose a version to add." if self.versions else "No compatible Forge versions were found.")

    def _worker_failed(self, message: str):
        self.search_button.setEnabled(True)
        self.status.setText(message)

    def request_selected_install(self):
        version = self.version.currentData()
        if self.project and isinstance(version, dict):
            self.install_button.setEnabled(False)
            self.status.setText("Checking required dependencies…")
            self.requested_install.emit(self.project, version)

    def reject(self):
        if self.worker and self.worker.isRunning():
            self.status.setText("Please wait for the Modrinth request to finish before closing.")
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            event.ignore()
            self.status.setText("Please wait for the Modrinth request to finish before closing.")
            return
        super().closeEvent(event)


class DependencyPlanWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, project: dict, version: dict):
        super().__init__()
        self.project = project
        self.version = version

    def run(self):
        try:
            dependencies = []
            for dependency in self.version.get("dependencies", []):
                if not isinstance(dependency, dict) or dependency.get("dependency_type") != "required":
                    continue
                project_id = dependency.get("project_id")
                if project_id == self.project.get("id"):
                    continue
                if not project_id:
                    label = dependency.get("file_name") or "an external required dependency"
                    raise LauncherError(f"Modrinth lists {label}, which cannot be resolved as a Modrinth project.")
                versions = get_project_versions(str(project_id))
                pinned_id = dependency.get("version_id")
                chosen = next((item for item in versions if item.get("id") == pinned_id), None)
                if chosen is None and not pinned_id and versions:
                    chosen = versions[0]
                if not chosen:
                    raise LauncherError(f"A required dependency ({project_id}) has no compatible Forge 1.20.1 version.")
                project = get_project(str(project_id))
                dependencies.append({"project": project, "version": chosen})
            self.succeeded.emit(dependencies)
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class MigrationWorker(QThread):
    progressed = Signal(str, int)
    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, source: Path, target: Path):
        super().__init__()
        self.source = source
        self.target = target

    def run(self):
        try:
            migrate_legacy_instance(self.source, self.target,
                                    lambda message, value: self.progressed.emit(message, value))
            self.succeeded.emit()
        except Exception as error:
            self.failed.emit(str(error))


class LoginWorker(QThread):
    device_url = Signal(str)
    executable_ready = Signal(str)
    output = Signal(str)
    progressed = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, executable: str, root: Path):
        super().__init__()
        self.executable = executable
        self.root = root

    def run(self):
        process = None
        output_lines: list[str] = []
        try:
            ensure_instance(self.root)
            if not self.executable or not Path(self.executable).is_file():
                self.progressed.emit("HeadlessMc was not found. Installing the official release...")
                self.executable = str(install_headlessmc(
                    self.root, lambda message, _percentage: self.progressed.emit(message)
                ))
                self.executable_ready.emit(self.executable)
            process = subprocess.Popen(
                hmc_login_command(self.executable),
                cwd=str(self.root),
                env=hmc_environment(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert process.stdout is not None
            for line in process.stdout:
                clean = line.strip()
                if not clean:
                    continue
                output_lines.append(clean)
                self.output.emit(clean)
                match = re.search(r"https://www\.microsoft\.com/link\?otc=\S+", clean)
                if match:
                    self.device_url.emit(match.group(0).rstrip(".,)"))
            result = process.wait()
            if result != 0:
                detail = "\\n".join(output_lines[-8:])
                raise LauncherError(f"HeadlessMC login failed with exit code {result}")
            accounts = hmc_accounts(self.executable, self.root)
            if not accounts:
                raise LauncherError("HeadlessMC login completed, but no Microsoft account was found")
            self.succeeded.emit(accounts)
        except Exception as error:
            if process is not None and process.poll() is None:
                process.kill()
            detail = "\n".join(output_lines[-8:])
            message = f"{type(error).__name__}: {error}"
            if detail and detail not in message:
                message += "\n" + detail
            self.failed.emit(message)


class OfflineLoginWorker(QThread):
    executable_ready = Signal(str)
    progressed = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, executable: str, root: Path, name: str, email: str, password: str):
        super().__init__()
        self.executable = executable
        self.root = root
        self.name = name
        self.email = email
        self.password = password

    def run(self):
        account_uuid = ""
        credential_key = ""
        account_created = False
        try:
            self.progressed.emit("Signing in to WibsVerse...")
            web_session = WibsVerseWebSession()
            account_html = web_session.login(self.email, self.password)
            linked_name = web_session.linked_minecraft_username(account_html)
            if linked_name and linked_name.casefold() != self.name.casefold():
                raise WibsVerseAuthError("This WibsVerse account is already linked to a different Minecraft username.")
            if not linked_name and not web_session.can_link_minecraft_profile(account_html):
                raise WibsVerseAuthError("Could not determine whether this WibsVerse account already has a Minecraft profile linked.")
            ensure_instance(self.root)
            if not self.executable or not Path(self.executable).is_file():
                self.progressed.emit("HeadlessMc was not found. Installing the official release...")
                self.executable = str(install_headlessmc(
                    self.root, lambda message, _percentage: self.progressed.emit(message)
                ))
                self.executable_ready.emit(self.executable)
            self.progressed.emit(f"Adding offline account {self.name}...")
            account = create_hmc_offline_account(self.root, self.name)
            account_created = True
            credential_key = account_credential_key({"mode": "offline", "uuid": account["uuid"]})
            save_password(credential_key, self.password)
            accounts = hmc_accounts(self.executable, self.root)
            if not any(account["name"] == self.name and account["mode"] == "offline" for account in accounts):
                raise LauncherError("HeadlessMC did not create the offline account")
            self.succeeded.emit({
                "accounts": accounts, "account": account, "email": self.email,
                "minecraft_linked": bool(linked_name),
            })
        except Exception as error:
            if account_created:
                try:
                    remove_hmc_offline_account(self.root, self.name)
                except Exception:
                    pass
            if credential_key:
                delete_password(credential_key)
            self.failed.emit(f"{type(error).__name__}: {error}")


class WibsVerseCredentialWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, account_key: str, minecraft_username: str, email: str, password: str):
        super().__init__()
        self.account_key = account_key
        self.minecraft_username = minecraft_username
        self.email = email
        self.password = password

    def run(self):
        try:
            session = WibsVerseWebSession()
            html = session.login(self.email, self.password)
            linked_name = session.linked_minecraft_username(html)
            if linked_name and linked_name.casefold() != self.minecraft_username.casefold():
                raise WibsVerseAuthError("This WibsVerse account is already linked to a different Minecraft username.")
            if not linked_name and not session.can_link_minecraft_profile(html):
                raise WibsVerseAuthError("Could not determine this WibsVerse account's Minecraft link status.")
            save_password(self.account_key, self.password)
            self.succeeded.emit(self.email)
        except Exception as error:
            self.failed.emit(str(error))


class RemoveAccountWorker(QThread):
    executable_ready = Signal(str)
    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, executable: str, root: Path, name: str):
        super().__init__()
        self.executable = executable
        self.root = root
        self.name = name

    def run(self):
        try:
            ensure_instance(self.root)
            if not self.executable or not Path(self.executable).is_file():
                self.executable = str(install_headlessmc(self.root))
                self.executable_ready.emit(self.executable)
            run_hmc(
                self.executable,
                self.root,
                hmc_remove_account_command(self.executable, self.name)[1:],
                timeout=30,
            )
            self.succeeded.emit()
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class LaunchWorker(QThread):
    progressed = Signal(str)
    executable_ready = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self, executable: str, root: Path, account_name: str, account_mode: str,
        account_uuid: str, join_server: bool, wibsverse_email: str = "", wibsverse_password: str = "",
    ):
        super().__init__()
        self.executable = executable
        self.root = root
        self.account_name = account_name
        self.account_mode = account_mode
        self.account_uuid = account_uuid
        self.join_server = join_server
        self.wibsverse_email = wibsverse_email
        self.wibsverse_password = wibsverse_password

    def run(self):
        process = None
        handoff = None
        try:
            ensure_instance(self.root)
            if not self.executable or not Path(self.executable).is_file():
                self.progressed.emit("HeadlessMc was not found. Installing the official release...")
                self.executable = str(install_headlessmc(self.root, lambda message, _percentage: self.progressed.emit(message)))
                self.executable_ready.emit(self.executable)
            ensure_java17(self.root, lambda message, _percentage: self.progressed.emit(message))
            run_hmc(self.executable, self.root,
                    hmc_select_account_command(self.executable, self.account_name)[1:], timeout=30)
            if not hmc_profile_is_pinned(self.root):
                self.progressed.emit(f"Installing Forge {MINECRAFT_VERSION}-{FORGE_VERSION} through HeadlessMC...")
                run_hmc(self.executable, self.root, hmc_profile_command(self.executable, self.root)[1:], timeout=1800)
                if not hmc_profile_is_pinned(self.root):
                    raise LauncherError(
                        f"HeadlessMC did not create the required Forge {MINECRAFT_VERSION}-{FORGE_VERSION} profile"
                    )
            self.progressed.emit("Starting Minecraft headlessly through HeadlessMC...")
            log_path = app_dir() / "headlessmc-launch.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            java_properties = None
            credential: GameAuthCredential | None = None
            link_code = None
            linked_name = None
            web_session = None
            needs_link = False
            if self.account_mode == "offline" and self.join_server:
                web_session = WibsVerseWebSession(timeout=8)
                account_html = web_session.login(self.wibsverse_email, self.wibsverse_password)
                linked_name = web_session.linked_minecraft_username(account_html)
                if linked_name and linked_name.casefold() != self.account_name.casefold():
                    raise LauncherError("This WibsVerse account is already linked to a different Minecraft username.")
                needs_link = not linked_name
                if needs_link:
                    if not web_session.can_link_minecraft_profile(account_html):
                        raise LauncherError("Could not determine whether this WibsVerse account already has a Minecraft profile linked.")
                    web_session.confirm_password(self.wibsverse_password)
                install_launcher_link_bridge(self.root)
                handoff = SsoHandoff.create(self.root, self.account_uuid, self.account_name, SERVER)
                java_properties = {
                    "wibs.sso.ipc": handoff.java_property,
                    "wibs.autojoin": "true",
                }
                if needs_link:
                    java_properties["wibs.linkaccount.file"] = str(
                        (handoff.directory / "link-code.json").resolve()
                    ).replace("\\", "/")
            elif self.account_mode == "offline":
                java_properties = {"wibs.autojoin": "false"}
            launch_env = hmc_environment(self.root, java_properties)
            log = log_path.open("ab")
            try:
                launched_at = time.time()
                process = subprocess.Popen(
                    hmc_launch_command(
                        self.executable,
                        self.account_name,
                        self.join_server and not (self.account_mode == "offline" and handoff is not None),
                    ),
                    cwd=str(self.root),
                    env=launch_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                log.close()
                raise
            log.close()
            if handoff is not None:
                self.progressed.emit("Completing WibsVerse SSO handoff...")
                process_id = wait_for_minecraft_process_id(process.pid, game_dir(self.root), launched_at)
                handoff.write_context(process_id)
                if not handoff.wait_until_ready(540):
                    raise LauncherError("WibsVerse SSO did not accept the launch context.")
                if needs_link and web_session is not None:
                    self.progressed.emit("Creating a single-use WibsVerse Minecraft link code...")
                    link_code = web_session.generate_link_code(self.wibsverse_password, confirmed=True)
                    handoff.write_link_code(link_code.code, link_code.expires_at)
                self.progressed.emit("Authenticating this offline account with WibsVerse...")
                credential = WibsVerseGameAuthClient(timeout=8).authenticate(self.account_name, self.wibsverse_password)
                if credential.runtime_uuid.replace("-", "").casefold() != self.account_uuid.replace("-", "").casefold():
                    raise LauncherError("WibsVerse returned a credential for a different Minecraft profile.")
                handoff.write_credential(credential)
                if not handoff.wait_until_consumed(45):
                    raise LauncherError("Minecraft did not consume the WibsVerse SSO credential.")
                if link_code:
                    handoff.cleanup_auth_files()
                else:
                    handoff.cleanup()
            self.succeeded.emit({
                "process": process,
                "sso_handoff": handoff if link_code else None,
                "minecraft_linked": bool(linked_name),
                "account_key": account_credential_key({"mode": self.account_mode, "uuid": self.account_uuid}),
                "account_name": self.account_name,
            } if handoff is not None else process)
        except Exception as error:
            if handoff is not None:
                handoff.cleanup()
            self.failed.emit(f"{type(error).__name__}: {error}")


class GameIconWorker(QThread):
    def __init__(self, process_id: int, icon_file: Path):
        super().__init__()
        self.process_id = process_id
        self.icon_file = icon_file

    def run(self):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if apply_game_window_icon(self.process_id, self.icon_file):
                return
            time.sleep(0.25)


class AvatarWorker(QThread):
    fetched = Signal(str, bytes)

    def __init__(self, uuid_text: str):
        super().__init__()
        self.uuid_text = uuid_text

    def run(self):
        try:
            data = skin_png_for_uuid(self.uuid_text, app_dir() / "avatars")
            if data:
                self.fetched.emit(self.uuid_text, data)
        except Exception:
            # Fallback avatar is intentional when offline, rate limited, or unsupported.
            pass


def anonymous_avatar() -> QIcon:
    """Grayscale pixel face when no skin is available."""
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(QColor("#818593"))
    p = QPainter(image)
    for x, y, w, h, color in (
        (1, 1, 6, 2, "#6B6F7A"), (1, 3, 6, 4, "#B6B6BD"),
        (1, 3, 2, 2, "#484B52"), (5, 3, 2, 2, "#484B52"),
        (3, 5, 2, 1, "#85878F"), (2, 6, 4, 1, "#666A72"),
    ):
        p.fillRect(x, y, w, h, QColor(color))
    p.end()
    return QIcon(QPixmap.fromImage(image).scaled(40, 40,
                 Qt.AspectRatioMode.IgnoreAspectRatio,
                 Qt.TransformationMode.FastTransformation))


def avatar_from_skin(data: bytes) -> QIcon | None:
    skin = QImage.fromData(data)
    if skin.isNull():
        return None
    # mc-heads returns a ready-to-display rendered head instead of a skin layout.
    if skin.width() == skin.height() and skin.width() < 64:
        return QIcon(QPixmap.fromImage(skin).scaled(
            40, 40, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation
        ))
    if skin.width() < 64 or skin.height() < 32 or skin.width() % 64:
        return None
    scale = skin.width() // 64
    if skin.height() < 32 * scale:
        return None
    face = QImage(8, 8, QImage.Format.Format_ARGB32)
    face.fill(Qt.GlobalColor.transparent)
    p = QPainter(face)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    p.drawImage(face.rect(), skin.copy(8 * scale, 8 * scale, 8 * scale, 8 * scale))
    # Minecraft skin outer head layer (hat / hair).
    p.drawImage(face.rect(), skin.copy(40 * scale, 8 * scale, 8 * scale, 8 * scale))
    p.end()
    rendered = QPixmap.fromImage(face).scaled(40, 40,
                  Qt.AspectRatioMode.IgnoreAspectRatio,
                  Qt.TransformationMode.FastTransformation)
    return QIcon(rendered)


class UpdateDialog(QDialog):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Updating Modpack")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(430)
        self.setObjectName("updateDialog")
        panel = QVBoxLayout(self)
        panel.setContentsMargins(26, 26, 26, 26)
        panel.setSpacing(14)
        title = QLabel("Updating Modpack")
        title.setObjectName("updateTitle")
        panel.addWidget(title)
        self.note = QLabel("Preparing your Wibsverse installation...")
        self.note.setWordWrap(True)
        panel.addWidget(self.note)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        panel.addWidget(self.progress)
        self.percent = QLabel("0%")
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight)
        panel.addWidget(self.percent)
        note = QLabel("Your worlds and screenshots are preserved. Please keep the launcher open.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        panel.addWidget(note)
        self.active = True

    def update_progress(self, message: str, percentage: int):
        self.note.setText(message)
        self.progress.setValue(max(0, min(100, percentage)))
        self.percent.setText(f"{self.progress.value()}%")

    def closeEvent(self, event):
        if self.active:
            event.ignore()
            return
        super().closeEvent(event)


class AccountHelpDialog(QDialog):
    """Shows HeadlessMc's Microsoft device-code login progress."""
    def __init__(
        self,
        parent: "Launcher",
        title: str = "Add Microsoft Account",
        description: str = "HeadlessMc will show a one-time Microsoft device code. Complete sign-in in your browser; no Prism window will open.",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 25, 28, 25)
        layout.setSpacing(14)
        title = QLabel("Sign in with Microsoft")
        title.setObjectName("updateTitle")
        layout.addWidget(title)
        info = QLabel(description)
        info.setWordWrap(True)
        layout.addWidget(info)
        self.status = QLabel("Starting HeadlessMc login...")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.url = QLabel("")
        self.url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url.setWordWrap(True)
        layout.addWidget(self.url)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self.reject)
        layout.addWidget(self.cancel)

    def show_device_url(self, url: str):
        self.status.setText("Complete Microsoft sign-in in your browser. The login will finish automatically.")
        self.url.setText(url)

    def show_output(self, text: str):
        if not self.url.text():
            self.status.setText(text)


class AddOfflineAccountDialog(QDialog):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Add Offline Account")
        self.setMinimumWidth(420)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(14)
        title = QLabel("Connect an Offline Account")
        title.setObjectName("updateTitle")
        layout.addWidget(title)
        explanation = QLabel(
            "Sign in with the WibsVerse account you have linked to this Minecraft username. "
            "The launcher stores its password in your operating system's credential vault."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        form = QFormLayout()
        self.minecraft_name = QLineEdit()
        self.minecraft_name.setPlaceholderText("Offline Minecraft username")
        form.addRow("Minecraft username", self.minecraft_name)
        self.email = QLineEdit()
        self.email.setPlaceholderText("WibsVerse email")
        form.addRow("WibsVerse email", self.email)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("WibsVerse password")
        form.addRow("WibsVerse password", self.password)
        layout.addLayout(form)
        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.add_button = buttons.addButton("Sign in & Add", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole).clicked.connect(self.reject)
        self.add_button.clicked.connect(self.accept)
        layout.addWidget(buttons)


class Launcher(QWidget):
    def __init__(self):
        super().__init__()
        app_dir().mkdir(parents=True, exist_ok=True)
        saved = load_json(app_dir() / "settings.json", {})
        self.settings: dict = saved if isinstance(saved, dict) else {}
        self._new_root_selection = "hmc_root" not in self.settings
        self.hmc_exe = str(self.settings.get("headlessmc_exe") or find_headlessmc() or "")
        self.root = Path(self.settings.get("hmc_root") or (app_dir() / "HeadlessMc"))
        self.selection: dict = self.settings.get("selected_account", {})
        if not isinstance(self.selection, dict):
            self.selection = {}
        self.release: Release | None = None
        self.task: UpdateWorker | None = None
        self.launcher_release: LauncherRelease | None = None
        self.launcher_release_task: LauncherReleaseWorker | None = None
        self.launcher_download_task: LauncherUpdateWorker | None = None
        self._launcher_update_prompted = False
        self.migration_task: MigrationWorker | None = None
        self.login_task: LoginWorker | None = None
        self.offline_task: OfflineLoginWorker | None = None
        self.remove_account_task: RemoveAccountWorker | None = None
        self.wibsverse_task: WibsVerseCredentialWorker | None = None
        self.launch_task: LaunchWorker | None = None
        self.game_process: subprocess.Popen | None = None
        self._console_file_identity: tuple[int, int] | None = None
        self._console_offset = 0
        self._console_initialized = False
        self.pending_link_handoff: SsoHandoff | None = None
        self.pending_link_account_key = ""
        self.update_dialog: UpdateDialog | None = None
        self.queued_release: Release | None = None
        self.game_icon_task: GameIconWorker | None = None
        self._has_accounts = False
        self._avatar_icon = anonymous_avatar()
        self._avatar_uuid = ""
        self._avatar_workers: list[AvatarWorker] = []
        self._selected_game = "demon_slayer"
        self.client_mods: list[ClientMod] = []
        self.client_mod_worker: ModrinthWorker | None = None
        self.pending_client_mod_request: tuple[str, tuple] | None = None
        self.dependency_plan_worker: DependencyPlanWorker | None = None
        self.pending_mod_install: tuple[dict, dict] | None = None
        self.add_mod_dialog: AddModDialog | None = None
        self._background_pixmap = QPixmap(str(ASSET_DIR / "wibsversebg.png"))
        self.setWindowTitle("Wibsverse Launcher")
        self.setObjectName("launcherWindow")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        icon_asset = launcher_icon_asset()
        if icon_asset.is_file():
            icon = QIcon(str(icon_asset))
            self.setWindowIcon(icon)
            application = QApplication.instance()
            if application is not None:
                application.setWindowIcon(icon)
        self.resize(1200, 760)
        self.setMinimumSize(900, 640)
        self._drag_offset = None
        self._ui()
        self.console_timer = QTimer(self)
        self.console_timer.setInterval(500)
        self.console_timer.timeout.connect(self._poll_console_log)
        self.link_watch_timer = QTimer(self)
        self.link_watch_timer.setInterval(500)
        self.link_watch_timer.timeout.connect(self._poll_link_status)
        self._refresh_accounts()
        self.update_installed_label()
        QTimer.singleShot(180, self._startup)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(INK))
        if not self._background_pixmap.isNull() and self.width() and self.height():
            size = max(640, int(self.height() * 1.04))
            art = self._background_pixmap.scaled(
                size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(int(self.width() * .365), 6, art)
        side = QLinearGradient(0, 0, self.width(), 0)
        side.setColorAt(0, QColor(16, 11, 18, 255))
        side.setColorAt(.36, QColor(16, 11, 18, 255))
        side.setColorAt(.58, QColor(16, 11, 18, 184))
        side.setColorAt(1, QColor(27, 8, 26, 37))
        painter.fillRect(self.rect(), side)
        vertical = QLinearGradient(0, 0, 0, self.height())
        vertical.setColorAt(0, QColor(16, 11, 18, 102))
        vertical.setColorAt(.45, QColor(16, 11, 18, 0))
        vertical.setColorAt(.76, QColor(16, 11, 18, 136))
        vertical.setColorAt(1, QColor(16, 11, 18, 255))
        painter.fillRect(self.rect(), vertical)
        painter.setPen(QColor("#392733"))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 10, 10)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= 32:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def _ui(self):
        self.setStyleSheet('''
          QWidget { background:transparent; color:#FFF1DF; font: 10pt "Segoe UI"; }
          QWidget#launcherWindow { background:#100B12; }
          QLabel#windowName { color:#877581; font-size:8pt; letter-spacing:2px; }
          QLabel#wordmark { color:#FFF1DF; font-family:"Georgia"; font-size:23pt; font-weight:400; }
          QLabel#heroTitle { color:#FFF1DF; font-family:"Cormorant Garamond"; font-size:38pt; font-weight:600; }
          QLabel#category { color:#B89758; font-size:8pt; letter-spacing:3px; }
          QLabel#description { color:#BAA6B4; font-size:11pt; }
          QLabel#caption { color:#FFF1DF; font-size:8pt; letter-spacing:2px; }
          QLabel#muted { color:#BAA6B4; font-size:8pt; }
          QLabel#version { color:#BAA6B4; font-size:8pt; }
          QLabel#status { color:#BAA6B4; font-size:9pt; }
          QLabel#ready { color:#93C7A1; font-size:8pt; font-weight:600; letter-spacing:2px; }
          QLabel#installedTitle { color:#FFF1DF; font-family:"Cormorant Garamond"; font-size:19pt; font-weight:600; }
          QLabel#updateTitle { font-size:18pt; font-weight:750; }
          QFrame#titleBar { background:rgba(16,11,18,238); }
          QFrame#navigation { background:rgba(22,14,23,232); border-bottom:1px solid #392733; }
          QFrame#launchPanel { background:rgba(25,15,26,245); border-top:1px solid #604435; }
          QFrame#footer { background:#110B13; border-top:1px solid #392733; }
          QPushButton, QToolButton { background:#211420; border:1px solid #392733;
              border-radius:8px; padding:8px 12px; }
          QPushButton:hover:enabled, QToolButton:hover { background:#392033; }
          QPushButton#play { background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #F258A1,stop:1 #C92371);
              border:1px solid #FF93C4; font-size:18pt; font-weight:800; letter-spacing:2px; border-radius:8px; padding:12px; }
          QPushButton#play:hover:enabled { background:#F258A1; }
          QPushButton#play:disabled { background:#653451; color:#B5A9B1; }
          QPushButton#primary { background:#BA277E; font-weight:650; }
          QToolButton#account { font-size:9pt; font-weight:600; padding:6px 10px;
              background:#2B1927; border:1px solid #6C3C54; border-radius:8px; }
          QToolButton#settings { font-size:14pt; padding:4px; }
          QToolButton#textLink { color:#FF92C4; background:transparent; border:0; padding:0; text-align:left; font-size:9pt; }
          QToolButton#textLink:hover { color:#FFB2D4; background:transparent; text-decoration:underline; }
          QToolButton#navTab { color:#BAA6B4; background:transparent; border:0; border-bottom:2px solid transparent;
              border-radius:0; padding:10px 14px; font-weight:600; }
          QToolButton#navTab:hover { color:#FFF1DF; background:rgba(57,39,51,100); }
          QToolButton#navTab:checked { color:#FFF1DF; border-bottom:2px solid #ED4194; }
          QToolButton#windowControl { background:transparent; border:0; border-radius:0; padding:0; color:#BAA6B4; }
          QToolButton#windowControl:hover { background:#392733; color:#FFF1DF; }
          QCheckBox { spacing:10px; color:#FFF1DF; font-size:9pt; }
          QCheckBox::indicator { width:18px; height:18px; border:1px solid #6C3C54; border-radius:4px; background:#211420; }
          QCheckBox::indicator:checked { background:#ED4194; border-color:#ED4194; }
          QMenu { background:#221722; border:1px solid #6C3C54; padding:7px; }
          QMenu::item { padding:10px 24px; border-radius:6px; }
          QMenu::item:selected { background:#6E315A; }
          QMenu::separator { height:1px; background:#54495B; margin:6px 8px; }
          QProgressBar { background:#37303F; border:0; border-radius:6px; height:17px; text-align:center; }
          QProgressBar::chunk { background:#D9439F; border-radius:6px; }
          QDialog { background:#1A1B27; }
          QLineEdit { background:#2A2936; border:1px solid #54495B; border-radius:8px; padding:12px; }
          QTableWidget { background:#100E14; alternate-background-color:#19141D; gridline-color:#392733;
              border:1px solid #392733; border-radius:8px; selection-background-color:#6E315A; }
          QHeaderView::section { background:#211420; color:#BAA6B4; border:0; border-bottom:1px solid #392733;
              padding:8px; font-weight:600; }
          QPlainTextEdit#consoleOutput { background:#100E14; color:#E6DDE5; border:1px solid #392733;
              border-radius:8px; padding:8px; font:9pt "Consolas"; selection-background-color:#8D3267; }
        ''')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 0, 1, 1)
        outer.setSpacing(0)

        titlebar = QFrame()
        titlebar.setObjectName("titleBar")
        titlebar.setFixedHeight(32)
        title_layout = QHBoxLayout(titlebar)
        title_layout.setContentsMargins(18, 0, 10, 0)
        window_name = QLabel(f"WIBSVERSE LAUNCHER  v{LAUNCHER_VERSION}")
        window_name.setObjectName("windowName")
        title_layout.addWidget(window_name)
        title_layout.addStretch()
        for glyph, tip, action in (("−", "Minimize", self.showMinimized), ("□", "Maximize", self._toggle_maximized), ("×", "Close", self.close)):
            control = QToolButton()
            control.setObjectName("windowControl")
            icon_name = {"−": "minus.svg", "□": "square.svg", "×": "close.svg"}[glyph]
            control.setIcon(QIcon(str(ASSET_DIR / icon_name)))
            control.setIconSize(QPixmap(14, 14).size())
            control.setText(glyph)
            control.setToolTip(tip)
            control.setFixedSize(34, 30)
            control.clicked.connect(action)
            title_layout.addWidget(control)
        outer.addWidget(titlebar)

        navigation = QFrame()
        navigation.setObjectName("navigation")
        navigation.setFixedHeight(72)
        toolbar = QHBoxLayout(navigation)
        toolbar.setContentsMargins(30, 0, 30, 0)
        toolbar.setSpacing(12)
        logo = QLabel('<a href="https://wibsverse.com/" style="text-decoration:none;">'
                      '<span style="color:#FFF1DF">wibsverse</span>'
                      '<span style="color:#ED4194">.</span></a>')
        logo.setObjectName("wordmark")
        logo.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        logo.setOpenExternalLinks(True)
        logo.setCursor(Qt.CursorShape.PointingHandCursor)
        toolbar.addWidget(logo)
        self.play_tab = QToolButton()
        self.play_tab.setObjectName("navTab")
        self.play_tab.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.play_tab.setIcon(QIcon(str(ASSET_DIR / "swords.svg")))
        self.play_tab.setText("Demon Slayer")
        self.play_tab.setCheckable(True)
        self.play_tab.setChecked(True)
        self.play_tab.clicked.connect(lambda: self._select_game("demon_slayer"))
        toolbar.addWidget(self.play_tab)
        self.jjk_tab = QToolButton()
        self.jjk_tab.setObjectName("navTab")
        self.jjk_tab.setText("Jujutsu Kaisen")
        self.jjk_tab.setCheckable(True)
        self.jjk_tab.clicked.connect(lambda: self._select_game("jujutsu_kaisen"))
        toolbar.addWidget(self.jjk_tab)
        self.solo_leveling_tab = QToolButton()
        self.solo_leveling_tab.setObjectName("navTab")
        self.solo_leveling_tab.setText("Solo Leveling")
        self.solo_leveling_tab.setCheckable(True)
        self.solo_leveling_tab.clicked.connect(lambda: self._select_game("solo_leveling"))
        toolbar.addWidget(self.solo_leveling_tab)
        toolbar.addStretch()
        self.account_button = QToolButton()
        self.account_button.setObjectName("account")
        self.account_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.account_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.account_button.setIconSize(QPixmap(56, 32).size())
        self.account_button.setFixedSize(218, 48)
        self.account_menu = QMenu(self)
        self.account_button.setMenu(self.account_menu)
        toolbar.addWidget(self.account_button)
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("settings")
        self.settings_button.setIcon(QIcon(str(ASSET_DIR / "settings.svg")))
        self.settings_button.setIconSize(QPixmap(20, 20).size())
        self.settings_button.setFixedSize(44, 44)
        self.settings_button.setToolTip("Settings")
        self.settings_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.settings_menu = QMenu(self)
        for label, callback in (
            ("Console", lambda: self._show_launcher_page(1)),
            ("Client Mods", lambda: self._show_launcher_page(2)),
            ("Check for Updates", self.check_updates),
            ("Check for Launcher Updates", self.check_launcher_updates),
            ("Set HeadlessMc Path...", self.select_hmc),
            ("Game Files", self.open_folder),
        ):
            self.settings_menu.addAction(label).triggered.connect(callback)
        self.settings_menu.addSeparator()
        self.settings_menu.addAction("HeadlessMc Data Folder").triggered.connect(self.open_hmc_folder)
        self.settings_button.setMenu(self.settings_menu)
        toolbar.addWidget(self.settings_button)
        outer.addWidget(navigation)

        showcase = QWidget()
        showcase.setMinimumHeight(300)
        showcase_layout = QHBoxLayout(showcase)
        showcase_layout.setContentsMargins(48, 28, 48, 26)
        showcase_layout.setSpacing(0)
        intro = QVBoxLayout()
        intro.setContentsMargins(0, 0, 0, 0)
        intro.setSpacing(0)
        logo_art = QLabel()
        logo_art.setPixmap(QPixmap(str(ASSET_DIR / "wibsversehero.png")).scaled(
            390, 130, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        logo_art.setFixedSize(390, 130)
        intro.addWidget(logo_art)
        category = QLabel("—   A MINECRAFT RPG ADVENTURE")
        category.setObjectName("category")
        intro.addWidget(category)
        intro.addSpacing(12)
        self.game_header = QLabel("Wib’s Demon Slayer")
        self.game_header.setObjectName("heroTitle")
        intro.addWidget(self.game_header)
        self.game_description = QLabel("Walk the Slayer path or embrace the Demon path. Master techniques, conquer dungeons and rise through the ranks.")
        self.game_description.setObjectName("description")
        self.game_description.setFixedWidth(425)
        self.game_description.setWordWrap(True)
        intro.addWidget(self.game_description)
        intro.addSpacing(18)
        guide = QToolButton()
        guide.setObjectName("textLink")
        guide.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        guide.setIcon(QIcon(str(ASSET_DIR / "book-open.svg")))
        guide.setIconSize(QPixmap(16, 16).size())
        guide.setText("Explore the world  ↗")
        guide.setCursor(Qt.CursorShape.PointingHandCursor)
        guide.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://map.wibsverse.com/demonslayer/")))
        intro.addWidget(guide)
        intro.addStretch()
        showcase_layout.addLayout(intro)
        showcase_layout.addStretch()
        caption = QLabel("TWO PATHS. ONE DESTINY.")
        caption.setObjectName("caption")
        showcase_layout.addWidget(caption, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        panel = QFrame()
        panel.setObjectName("launchPanel")
        panel.setFixedHeight(122)
        panel_layout = QHBoxLayout(panel)
        panel_layout.setContentsMargins(48, 14, 48, 14)
        panel_layout.setSpacing(0)
        install_column = QVBoxLayout()
        install_column.setSpacing(5)
        ready_row = QWidget()
        ready_layout = QHBoxLayout(ready_row)
        ready_layout.setContentsMargins(0, 0, 0, 0)
        ready_layout.setSpacing(8)
        ready_icon = QLabel()
        ready_icon.setPixmap(QIcon(str(ASSET_DIR / "ready-indicator.svg")).pixmap(6, 6))
        ready_layout.addWidget(ready_icon)
        ready = QLabel("READY TO PLAY")
        ready.setObjectName("ready")
        ready_layout.addWidget(ready)
        ready_layout.addStretch()
        install_column.addWidget(ready_row)
        self.installed_label = QLabel("Checking installation...")
        self.installed_label.setObjectName("installedTitle")
        install_column.addWidget(self.installed_label)
        self.version_detail = QLabel("Modpack 2.3.0  ·  Latest version installed")
        self.version_detail.setObjectName("muted")
        install_column.addWidget(self.version_detail)
        panel_layout.addLayout(install_column, 1)

        options = QVBoxLayout()
        options.setSpacing(7)
        self.server_checkbox = QCheckBox("Auto join server")
        self.server_checkbox.setStyleSheet(
            f'QCheckBox::indicator:checked {{ image: url("{(ASSET_DIR / "check.svg").as_posix()}"); }}'
        )
        self.server_checkbox.setChecked(bool(self.settings.get("join_server", True)))
        self.server_checkbox.toggled.connect(lambda _: self._persist())
        options.addWidget(self.server_checkbox)
        server = QLabel("play.wibsverse.com")
        server.setObjectName("muted")
        options.addWidget(server)
        panel_layout.addLayout(options, 1)

        action_column = QVBoxLayout()
        action_column.setSpacing(5)
        self.status = QLabel("Checking for updates...")
        self.status.setObjectName("status")
        self.play_button = QPushButton(" PLAY")
        self.play_button.setObjectName("play")
        self.play_button.setIcon(QIcon(str(ASSET_DIR / "play.svg")))
        self.play_button.setIconSize(QPixmap(21, 21).size())
        self.play_button.setFixedSize(290, 64)
        self.play_button.clicked.connect(self.play)
        action_column.addWidget(self.play_button)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        action_column.addWidget(self.status)
        panel_layout.addLayout(action_column)
        self.add_account_button = QPushButton("+   ADD ACCOUNT")
        self.add_account_button.setObjectName("play")
        self.add_account_button.clicked.connect(self.show_add_account_dialog)
        self.add_account_button.hide()
        action_column.addWidget(self.add_account_button)
        self.page_stack = QStackedWidget()
        home_page = QWidget()
        home_layout = QVBoxLayout(home_page)
        home_layout.setContentsMargins(0, 0, 0, 0)
        home_layout.setSpacing(0)
        home_layout.addWidget(showcase, 1)
        home_layout.addWidget(panel)
        self.page_stack.addWidget(home_page)

        console_page = QWidget()
        console_layout = QVBoxLayout(console_page)
        console_layout.setContentsMargins(34, 24, 34, 24)
        console_layout.setSpacing(12)
        console_header = QHBoxLayout()
        console_back_button = QPushButton("←  Back to Launcher")
        console_back_button.clicked.connect(lambda: self._show_launcher_page(0))
        console_header.addWidget(console_back_button)
        console_heading = QLabel("GAME CONSOLE")
        console_heading.setObjectName("category")
        console_header.addWidget(console_heading)
        console_header.addStretch()
        self.console_clear_button = QPushButton("Clear Console")
        self.console_clear_button.clicked.connect(self._clear_console)
        console_header.addWidget(self.console_clear_button)
        self.console_copy_button = QPushButton("Copy to Clipboard")
        self.console_copy_button.clicked.connect(self._copy_console)
        console_header.addWidget(self.console_copy_button)
        console_layout.addLayout(console_header)
        self.console_log_label = QLabel(str(game_dir(self._game_root()) / "logs" / "latest.log"))
        self.console_log_label.setObjectName("muted")
        self.console_log_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        console_layout.addWidget(self.console_log_label)
        self.console_output = QPlainTextEdit()
        self.console_output.setObjectName("consoleOutput")
        self.console_output.setReadOnly(True)
        self.console_output.setMaximumBlockCount(5000)
        self.console_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        console_layout.addWidget(self.console_output, 1)
        self.console_status = QLabel("Waiting for Minecraft log output…")
        self.console_status.setObjectName("muted")
        console_layout.addWidget(self.console_status)
        self.page_stack.addWidget(console_page)

        client_mods_page = QWidget()
        client_mods_layout = QVBoxLayout(client_mods_page)
        client_mods_layout.setContentsMargins(48, 36, 48, 36)
        client_mods_layout.setSpacing(14)
        mods_header = QHBoxLayout()
        mods_back_button = QPushButton("←  Back to Launcher")
        mods_back_button.clicked.connect(lambda: self._show_launcher_page(0))
        mods_header.addWidget(mods_back_button)
        mods_header.addStretch()
        client_mods_layout.insertLayout(0, mods_header)
        mods_heading = QLabel("CLIENT MODS")
        mods_heading.setObjectName("category")
        client_mods_layout.addWidget(mods_heading)
        mods_title = QLabel("Customize your client")
        mods_title.setObjectName("heroTitle")
        client_mods_layout.addWidget(mods_title)
        mods_description = QLabel("Manage client-side Minecraft mods in your Wibsverse game folder.")
        mods_description.setObjectName("description")
        client_mods_layout.addWidget(mods_description)
        mods_controls = QHBoxLayout()
        self.mods_search = QLineEdit()
        self.mods_search.setPlaceholderText("Search mods…")
        self.mods_search.textChanged.connect(self._filter_mod_table)
        mods_controls.addWidget(self.mods_search, 1)
        self.add_mod_button = QPushButton("+  Add Mod")
        self.add_mod_button.setObjectName("primary")
        self.add_mod_button.clicked.connect(self.show_add_mod_dialog)
        mods_controls.addWidget(self.add_mod_button)
        self.refresh_mods_button = QPushButton("Refresh")
        self.refresh_mods_button.clicked.connect(self._refresh_client_mods)
        mods_controls.addWidget(self.refresh_mods_button)
        client_mods_layout.addLayout(mods_controls)
        self.mods_table = QTableWidget(0, 3)
        self.mods_table.setHorizontalHeaderLabels(["Mod", "Version", "Enabled"])
        self.mods_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.mods_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.mods_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.mods_table.verticalHeader().setVisible(False)
        self.mods_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.mods_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.mods_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.mods_table.itemChanged.connect(self._client_mod_item_changed)
        self.mods_table.itemSelectionChanged.connect(self._update_mod_actions)
        client_mods_layout.addWidget(self.mods_table, 1)
        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("Select a mod for more actions."))
        action_row.addStretch()
        self.check_mod_updates_button = QPushButton("Check for Updates")
        self.check_mod_updates_button.clicked.connect(self.check_selected_mod_updates)
        action_row.addWidget(self.check_mod_updates_button)
        self.check_mod_dependencies_button = QPushButton("Check Dependencies")
        self.check_mod_dependencies_button.clicked.connect(self.check_selected_mod_dependencies)
        action_row.addWidget(self.check_mod_dependencies_button)
        self.delete_mod_button = QPushButton("Delete Mod")
        self.delete_mod_button.clicked.connect(self.delete_selected_mod)
        action_row.addWidget(self.delete_mod_button)
        self.check_mod_updates_button.setEnabled(False)
        self.check_mod_dependencies_button.setEnabled(False)
        self.delete_mod_button.setEnabled(False)
        client_mods_layout.addLayout(action_row)
        self.mods_status = QLabel("Extra client mods are detected from the mods folder.")
        self.mods_status.setObjectName("status")
        client_mods_layout.addWidget(self.mods_status)
        self.page_stack.addWidget(client_mods_page)
        outer.addWidget(self.page_stack, 1)

        footer_frame = QFrame()
        footer_frame.setObjectName("footer")
        footer_frame.setFixedHeight(44)
        footer = QHBoxLayout(footer_frame)
        footer.setContentsMargins(30, 0, 30, 0)
        minecraft_label = QLabel("Minecraft 1.20.1")
        minecraft_label.setObjectName("muted")
        footer.addWidget(minecraft_label)
        divider = QLabel("│")
        divider.setStyleSheet("color:#392733")
        footer.addWidget(divider)
        forge_label = QLabel("Forge 47.4.23")
        forge_label.setObjectName("muted")
        footer.addWidget(forge_label)
        footer.addStretch()
        help_label = QLabel("Need a hand?")
        help_label.setObjectName("muted")
        footer.addWidget(help_label)
        community = QToolButton()
        community.setObjectName("textLink")
        community.setText("Join the community  ↗")
        community.setCursor(Qt.CursorShape.PointingHandCursor)
        community.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://discord.wibsverse.com/")))
        footer.addWidget(community)
        outer.addWidget(footer_frame)

    def _toggle_maximized(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def _show_launcher_page(self, index: int):
        self.page_stack.setCurrentIndex(index)
        for game, tab in self._game_tabs():
            tab.setChecked(game == self._selected_game)
        if index == 1:
            self._update_console_context()
            self.console_timer.start()
            self._poll_console_log()
        else:
            self.console_timer.stop()
        if index == 2:
            self._refresh_client_mods()

    def _game_tabs(self):
        return (
            ("demon_slayer", self.play_tab),
            ("jujutsu_kaisen", self.jjk_tab),
            ("solo_leveling", self.solo_leveling_tab),
        )

    def _game_root(self, game: str | None = None) -> Path:
        game = game or self._selected_game
        if game == "demon_slayer":
            return self.root
        return self.root / "games" / game

    def _update_console_context(self):
        path = game_dir(self._game_root()) / "logs" / "latest.log"
        self.console_log_label.setText(str(path))
        self._console_file_identity = None
        self._console_offset = 0
        self._console_initialized = False
        self.console_output.clear()
        self.console_status.setText(f"Waiting for {self._selected_game.replace('_', ' ')} log output…")

    def _select_game(self, game: str):
        self._selected_game = game
        games = {
            "demon_slayer": {
                "title": "Wib’s Demon Slayer",
                "description": "Walk the Slayer path or embrace the Demon path. Master techniques, conquer dungeons and rise through the ranks.",
                "background": "wibsversebg.png",
                "button": " PLAY",
                "available": True,
            },
            "jujutsu_kaisen": {
                "title": "Jujutsu Kaisen",
                "description": "Enter a cursed world of techniques, clans, bosses and competitive progression.",
                "background": "wibsversebg_jjk.png",
                "button": "Coming Soon",
                "available": False,
            },
            "solo_leveling": {
                "title": "Solo Leveling",
                "description": "Awaken your power, clear dungeons and climb through a progression-driven RPG world.",
                "background": "wibsversebg_sl.png",
                "button": "Coming Soon",
                "available": False,
            },
        }[game]
        self.game_header.setText(games["title"])
        self.game_description.setText(games["description"])
        self._background_pixmap = QPixmap(str(ASSET_DIR / games["background"]))
        self.play_button.setText(games["button"])
        self.play_button.setIcon(QIcon(str(ASSET_DIR / "play.svg")) if games["available"] else QIcon())
        self.play_button.setEnabled(games["available"])
        self.status.setText("Checking for updates..." if games["available"] else "Modpack coming soon")
        self._update_account_cta()
        self._set_busy(self.task is not None)
        for tab_game, tab in self._game_tabs():
            tab.setChecked(tab_game == game)
        self.page_stack.setCurrentIndex(0)
        if hasattr(self, "console_log_label"):
            self._update_console_context()
        self.update()

    def open_mods_folder(self):
        folder = game_dir(self._game_root()) / "mods"
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _refresh_client_mods(self):
        try:
            self.client_mods = list_custom_mods(self._game_root(), read_installed(self._game_root()))
        except OSError as error:
            self.mods_status.setText(f"Could not read the mods folder: {error}")
            return
        self.mods_table.blockSignals(True)
        self.mods_table.setRowCount(0)
        for mod in self.client_mods:
            row = self.mods_table.rowCount()
            self.mods_table.insertRow(row)
            name = QTableWidgetItem(mod.filename)
            name.setData(Qt.ItemDataRole.UserRole, mod)
            self.mods_table.setItem(row, 0, name)
            version = mod.version_number or ("Modrinth" if mod.project_id else "Manual")
            self.mods_table.setItem(row, 1, QTableWidgetItem(version))
            enabled = QTableWidgetItem()
            enabled.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            enabled.setCheckState(Qt.CheckState.Checked if mod.enabled else Qt.CheckState.Unchecked)
            self.mods_table.setItem(row, 2, enabled)
        self.mods_table.blockSignals(False)
        self._filter_mod_table(self.mods_search.text())
        self._update_mod_actions()
        self.mods_status.setText(f"{len(self.client_mods)} extra mod(s) found. Modpack mods are hidden from this list.")

    def _filter_mod_table(self, query: str):
        words = [word.casefold() for word in query.split()]
        for row in range(self.mods_table.rowCount()):
            name = self.mods_table.item(row, 0).text().casefold()
            version = self.mods_table.item(row, 1).text().casefold()
            haystack = name + " " + version
            self.mods_table.setRowHidden(row, not all(word in haystack for word in words))

    def _selected_client_mod(self) -> ClientMod | None:
        row = self.mods_table.currentRow()
        if row < 0:
            return None
        item = self.mods_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update_mod_actions(self):
        selected = self._selected_client_mod() is not None
        self.delete_mod_button.setEnabled(selected)
        self.check_mod_updates_button.setEnabled(selected)
        self.check_mod_dependencies_button.setEnabled(selected)

    def _client_mod_item_changed(self, item: QTableWidgetItem):
        if item.column() != 2:
            return
        row = item.row()
        name_item = self.mods_table.item(row, 0)
        mod = name_item.data(Qt.ItemDataRole.UserRole) if name_item else None
        if not isinstance(mod, ClientMod):
            return
        if self.game_process is not None and self.game_process.poll() is None:
            self.mods_table.blockSignals(True)
            item.setCheckState(Qt.CheckState.Checked if mod.enabled else Qt.CheckState.Unchecked)
            self.mods_table.blockSignals(False)
            QMessageBox.information(self, "Minecraft is running", "Close Minecraft before changing client mods.")
            return
        try:
            set_mod_enabled(self._game_root(), mod, item.checkState() == Qt.CheckState.Checked)
            self._refresh_client_mods()
            self.mods_status.setText(f"{mod.filename} {'enabled' if item.checkState() == Qt.CheckState.Checked else 'disabled'}.")
        except (OSError, LauncherError) as error:
            QMessageBox.warning(self, "Could not change mod", str(error))
            self._refresh_client_mods()

    def delete_selected_mod(self):
        mod = self._selected_client_mod()
        if mod is None:
            return
        if self.game_process is not None and self.game_process.poll() is None:
            QMessageBox.information(self, "Minecraft is running", "Close Minecraft before deleting mods.")
            return
        answer = QMessageBox.question(
            self, "Delete Mod", f"Permanently delete {mod.filename}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_mod(self._game_root(), mod)
            self._refresh_client_mods()
            self.mods_status.setText(f"Deleted {mod.filename}.")
        except OSError as error:
            QMessageBox.warning(self, "Could not delete mod", str(error))

    def show_add_mod_dialog(self):
        if self.game_process is not None and self.game_process.poll() is None:
            QMessageBox.information(self, "Minecraft is running", "Close Minecraft before adding mods.")
            return
        self.add_mod_dialog = AddModDialog(self)
        self.add_mod_dialog.requested_install.connect(self._prepare_mod_install)
        self.add_mod_dialog.exec()
        self.add_mod_dialog = None
        self.pending_mod_install = None

    def _prepare_mod_install(self, project: dict, version: dict):
        if self.dependency_plan_worker and self.dependency_plan_worker.isRunning():
            return
        self.pending_mod_install = (project, version)
        self.dependency_plan_worker = DependencyPlanWorker(project, version)
        self.dependency_plan_worker.succeeded.connect(self._mod_dependency_plan_ready)
        self.dependency_plan_worker.failed.connect(self._mod_dependency_plan_failed)
        self.dependency_plan_worker.start()

    def _mod_dependency_plan_ready(self, dependencies: object):
        if not isinstance(dependencies, list) or self.pending_mod_install is None or self.add_mod_dialog is None:
            return
        project, version = self.pending_mod_install
        dependency_names = [str(item["project"].get("title") or item["project"].get("slug") or "Required dependency") for item in dependencies]
        if dependency_names:
            answer = QMessageBox.question(
                self, "Required Dependencies",
                f"{project.get('title', 'This mod')} requires:\n\n" + "\n".join(f"• {name}" for name in dependency_names)
                + "\n\nInstall these compatible dependencies too?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                if self.add_mod_dialog:
                    self.add_mod_dialog.install_button.setEnabled(True)
                    self.add_mod_dialog.status.setText("Add cancelled. You can choose another version or try again.")
                self.pending_mod_install = None
                return
        if self.add_mod_dialog:
            self.add_mod_dialog.status.setText("Downloading mod and required dependencies…")
        self._start_client_mod_worker("install", self._game_root(), project, version, dependencies)

    def _mod_dependency_plan_failed(self, message: str):
        if self.add_mod_dialog:
            self.add_mod_dialog.install_button.setEnabled(True)
            self.add_mod_dialog.status.setText("Could not check dependencies: " + message)

    def _start_client_mod_worker(self, operation: str, *args):
        if operation == "install" and self.game_process is not None and self.game_process.poll() is None:
            message = "Close Minecraft before installing or updating mods."
            if self.add_mod_dialog:
                self.add_mod_dialog.install_button.setEnabled(True)
                self.add_mod_dialog.status.setText(message)
            else:
                QMessageBox.information(self, "Minecraft is running", message)
            return
        if self.client_mod_worker and self.client_mod_worker.isRunning():
            self.pending_client_mod_request = (operation, args)
            return
        self.mods_status.setText("Contacting Modrinth…")
        self.client_mod_worker = ModrinthWorker(operation, *args)
        self.client_mod_worker.succeeded.connect(lambda result, op=operation: self._client_mod_worker_succeeded(op, result))
        self.client_mod_worker.failed.connect(lambda message, op=operation: self._client_mod_worker_failed(op, message))
        self.client_mod_worker.finished.connect(self._client_mod_worker_finished)
        self.client_mod_worker.start()

    def _client_mod_worker_failed(self, operation: str, message: str):
        if operation == "install" and self.add_mod_dialog:
            self.add_mod_dialog.install_button.setEnabled(True)
            self.add_mod_dialog.status.setText("Could not install mod: " + message)
        else:
            QMessageBox.warning(self, "Modrinth Request Failed", message)

    def _client_mod_worker_finished(self):
        self.client_mod_worker = None
        if self.pending_client_mod_request:
            operation, args = self.pending_client_mod_request
            self.pending_client_mod_request = None
            QTimer.singleShot(0, lambda: self._start_client_mod_worker(operation, *args))

    def check_selected_mod_updates(self):
        mod = self._selected_client_mod()
        if mod:
            self._start_client_mod_worker("update", self._game_root(), mod)

    def check_selected_mod_dependencies(self):
        mod = self._selected_client_mod()
        if not mod:
            return
        if mod.project_id:
            self._start_client_mod_worker("dependencies", mod.project_id)
        else:
            self._start_client_mod_worker("lookup", self._game_root(), mod)

    def _client_mod_worker_succeeded(self, operation: str, result: object):
        if operation == "lookup":
            if not isinstance(result, dict) or not result.get("project"):
                QMessageBox.information(self, "Modrinth Match Not Found", "This JAR was not found on Modrinth.")
            else:
                self._refresh_client_mods()
                project_id = str(result["project"].get("id") or "")
                QTimer.singleShot(0, lambda: self._start_client_mod_worker("dependencies", project_id))
            return
        self._dispatch_client_mod_worker_result(operation, result)

    def _dispatch_client_mod_worker_result(self, operation: str, result: object):
        """Dispatch Modrinth worker results; kept separate for easy UI updates."""
        if operation == "install":
            self.pending_mod_install = None
            self._refresh_client_mods()
            if self.add_mod_dialog:
                self.add_mod_dialog.accept()
            QMessageBox.information(self, "Mod Added", "The selected mod and its required dependencies were added.")
        elif operation == "update":
            if not isinstance(result, dict) or not result.get("project"):
                QMessageBox.information(self, "Modrinth Match Not Found", "This JAR was not found on Modrinth, so update information is unavailable.")
                return
            project, version = result.get("project"), result.get("version")
            if not version:
                QMessageBox.information(self, "No Compatible Versions", "No Forge 1.20.1 version is available for this mod.")
            elif not result.get("has_update"):
                QMessageBox.information(self, "No Update Available", f"{project.get('title', 'This mod')} is up to date ({version.get('version_number', 'latest')}).")
            else:
                answer = QMessageBox.question(self, "Mod Update Available", f"{project.get('title', 'This mod')} {version.get('version_number', '')} is available. Replace the current JAR?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if answer == QMessageBox.StandardButton.Yes:
                    mod = result.get("mod")
                    root = result.get("root", self._game_root())
                    QTimer.singleShot(0, lambda: self._start_client_mod_worker("install", root, project, version, [], mod))
        elif operation == "dependencies":
            projects = result.get("projects", []) if isinstance(result, dict) else []
            versions = result.get("versions", []) if isinstance(result, dict) else []
            names = [str(item.get("title", item.get("slug", item.get("id", "Dependency")))) for item in projects if isinstance(item, dict)]
            names += [str(item.get("name", item.get("version_number", "Version"))) for item in versions if isinstance(item, dict)]
            QMessageBox.information(self, "Mod Dependencies", "\n".join(dict.fromkeys(names)) if names else "Modrinth lists no dependencies for this project.")

    def _clear_console(self):
        self.console_output.clear()

    def _copy_console(self):
        QApplication.clipboard().setText(self.console_output.toPlainText())

    def _prepare_console_for_launch(self):
        """Start a fresh visible console without modifying Minecraft's log file."""
        self.console_output.clear()
        path = game_dir(self._game_root()) / "logs" / "latest.log"
        try:
            info = path.stat()
        except OSError:
            self._console_file_identity = None
            self._console_offset = 0
            self._console_initialized = True
            return
        self._console_file_identity = (info.st_dev, info.st_ino)
        self._console_offset = info.st_size
        self._console_initialized = True

    def _append_console_output(self, text: str):
        if not text:
            return
        scrollbar = self.console_output.verticalScrollBar()
        follow = scrollbar.value() >= scrollbar.maximum() - 2
        cursor = self.console_output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.console_output.setTextCursor(cursor)
        if follow:
            self.console_output.ensureCursorVisible()

    def _poll_console_log(self):
        if self.page_stack.currentIndex() != 1:
            return
        path = game_dir(self._game_root()) / "logs" / "latest.log"
        self.console_log_label.setText(str(path))
        running = self._selected_game == "demon_slayer" \
            and self.game_process is not None and self.game_process.poll() is None
        try:
            info = path.stat()
        except OSError:
            self.console_status.setText(
                "Waiting for Minecraft to create latest.log…" if running else "No Minecraft log has been created yet."
            )
            return

        identity = (info.st_dev, info.st_ino)
        rotated = self._console_file_identity is not None and identity != self._console_file_identity
        truncated = self._console_file_identity == identity and info.st_size < self._console_offset
        if rotated or truncated:
            self.console_output.clear()
            self._console_offset = 0
            self._console_initialized = True
        elif self._console_file_identity is None:
            self._console_offset = 0
        self._console_file_identity = identity

        try:
            with path.open("rb") as log_file:
                if not self._console_initialized:
                    # On launcher startup, show a bounded tail of the previous run.
                    start = max(0, info.st_size - 512 * 1024)
                    log_file.seek(start)
                    if start:
                        log_file.readline()
                    self._console_initialized = True
                else:
                    log_file.seek(self._console_offset)
                chunk = log_file.read(256 * 1024)
                self._console_offset = log_file.tell()
            self._append_console_output(chunk.decode("utf-8", errors="replace"))
        except OSError:
            # The game may replace/rotate latest.log while it is being read.
            pass

        self.console_status.setText(
            "Minecraft is running · Live log" if running else "Latest game log"
        )

    def _poll_link_status(self):
        handoff = self.pending_link_handoff
        if handoff is None:
            self.link_watch_timer.stop()
            return
        status_path = handoff.directory / "link-status.json"
        try:
            status = load_json(status_path, {})
        except (OSError, ValueError, TypeError):
            status = {}
        if isinstance(status, dict) and status.get("session_id") == handoff.session_id:
            if status.get("status") == "linked":
                profiles = self._account_profiles()
                profile = profiles.get(self.pending_link_account_key)
                if isinstance(profile, dict):
                    profile["minecraft_linked"] = True
                    profile["linked_minecraft_username"] = status.get("minecraft_username", "")
                    profiles[self.pending_link_account_key] = profile
                    self._persist()
                self.status.setText("WibsVerse Minecraft account linked.")
                handoff.cleanup()
                self.pending_link_handoff = None
                self.pending_link_account_key = ""
                self.link_watch_timer.stop()
                return
            if status.get("status") == "rejected":
                self.status.setText("WibsVerse did not accept the Minecraft link code. Re-link the account in settings.")
                handoff.cleanup()
                self.pending_link_handoff = None
                self.pending_link_account_key = ""
                self.link_watch_timer.stop()
                return
        if handoff.link_code_expires_at_epoch_seconds and handoff.link_code_expires_at_epoch_seconds < int(time.time()):
            self.status.setText("WibsVerse link code expired before linking completed. Launch again to retry.")
            handoff.cleanup()
            self.pending_link_handoff = None
            self.pending_link_account_key = ""
            self.link_watch_timer.stop()

    def _startup(self):
        old_root = legacy_prism_data_dir()
        target = self.root
        old_game = old_root / "instances" / "wibsverse-demon-slayer" / ".minecraft"
        if self._new_root_selection and old_game.exists() and not game_dir(target).exists():
            answer = QMessageBox.question(
                self, "Keep your Wibsverse worlds",
                "The previous Wibsverse launcher used a separate Prism folder.\n\n"
                "Copy your existing Wibsverse modpack and worlds into HeadlessMc?\n\n"
                "Your old installation will remain untouched as a backup.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.migration_task = MigrationWorker(old_root, target)
                self.update_dialog = UpdateDialog(self)
                self.update_dialog.setWindowTitle("Preparing Wibsverse")
                self.update_dialog.findChild(QLabel, "updateTitle").setText("Preparing Wibsverse")
                self.update_dialog.show()
                self.migration_task.progressed.connect(self._progress)
                self.migration_task.succeeded.connect(lambda: self._migration_done(target))
                self.migration_task.failed.connect(lambda error: self._migration_failed(old_root, error))
                self.migration_task.finished.connect(self._migration_finished)
                self.migration_task.start()
                return
        self._after_startup()

    def _migration_done(self, target: Path):
        self.root = target
        self._finish_update_dialog(True)

    def _migration_failed(self, old_root: Path, message: str):
        self._finish_update_dialog(False)
        QMessageBox.warning(self, "Migration failed", message + "\n\nThe original Wibsverse installation remains available.")

    def _migration_finished(self):
        self.migration_task = None
        self._after_startup()

    def _after_startup(self):
        self._persist()
        self._refresh_accounts()
        self.update_installed_label()
        self.check_launcher_updates()
        self.check_updates()

    def _persist(self):
        self.settings["headlessmc_exe"] = self.hmc_exe
        self.settings["hmc_root"] = str(self.root)
        self.settings["selected_account"] = dict(self.selection)
        if hasattr(self, "server_checkbox"):
            self.settings["join_server"] = self.server_checkbox.isChecked()
        atomic_json(app_dir() / "settings.json", self.settings)

    def _available_accounts(self) -> list[dict]:
        if not self.hmc_exe or not Path(self.hmc_exe).is_file():
            return []
        try:
            return hmc_accounts(self.hmc_exe, self.root)
        except LauncherError:
            return []

    def _refresh_accounts(self):
        available = self._available_accounts()
        self._has_accounts = bool(available)
        if not any(a["mode"] == self.selection.get("mode") and a["name"] == self.selection.get("name") for a in available):
            self.selection = dict(next((a for a in available if a["mode"] == "microsoft"),
                                       available[0] if available else {}))
        else:
            self.selection = dict(next(a for a in available if a["mode"] == self.selection.get("mode")
                                       and a["name"] == self.selection.get("name")))
        self._rebuild_account_menu(available)
        self._display_selected_account()
        self._update_account_cta()
        self._persist()

    def _update_account_cta(self):
        if self._selected_game == "demon_slayer":
            self.play_button.setVisible(self._has_accounts)
            self.add_account_button.setVisible(not self._has_accounts)
        else:
            self.play_button.setVisible(True)
            self.add_account_button.setVisible(False)

    def _rebuild_account_menu(self, available: list[dict]):
        menu = self.account_menu
        menu.clear()
        menu.addSection("Minecraft accounts")
        if not available:
            nothing = menu.addAction("No accounts added yet")
            nothing.setEnabled(False)
        for a in available:
            title = a["name"]
            act = menu.addAction(title)
            act.setCheckable(True)
            act.setChecked(a["mode"] == self.selection.get("mode") and a["name"] == self.selection.get("name"))
            act.triggered.connect(lambda checked=False, entry=dict(a): self._choose_account(entry))
        menu.addSeparator()
        menu.addAction("+  Add account...").triggered.connect(self.show_add_account_dialog)
        if available:
            menu.addAction("Remove account...").triggered.connect(self.remove_account)
            menu.addAction("Account settings...").triggered.connect(self.edit_account_settings)
        menu.addAction("Refresh accounts").triggered.connect(self._refresh_accounts)

    def _choose_account(self, account: dict):
        self.selection = dict(account)
        self._display_selected_account()
        QTimer.singleShot(0, lambda: self._rebuild_account_menu(self._available_accounts()))
        self._persist()

    def _display_selected_account(self):
        name = self.selection.get("name", "")
        self.account_button.setText(name if name else "Accounts")
        uuid_text = self.selection.get("uuid", "")
        if uuid_text != self._avatar_uuid:
            self._avatar_uuid = uuid_text
            self._avatar_icon = anonymous_avatar()
            if uuid_text:
                worker = AvatarWorker(uuid_text)
                self._avatar_workers.append(worker)
                worker.fetched.connect(self._avatar_fetched)
                worker.finished.connect(lambda w=worker: self._avatar_finished(w))
                worker.start()
        self._refresh_account_button_icon()

    def _refresh_account_button_icon(self):
        icon_image = QImage(56, 32, QImage.Format.Format_ARGB32)
        icon_image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(icon_image)
        painter.drawPixmap(0, 0, self._avatar_icon.pixmap(32, 32))
        chevron = QIcon(str(ASSET_DIR / "chevron-down.svg")).pixmap(16, 16)
        painter.drawPixmap(40, 8, chevron)
        painter.end()
        self.account_button.setIcon(QIcon(QPixmap.fromImage(icon_image)))

    def _avatar_fetched(self, uuid_text: str, data: bytes):
        if uuid_text == self._avatar_uuid:
            result = avatar_from_skin(data)
            if result is not None:
                self._avatar_icon = result
                self._refresh_account_button_icon()

    def _avatar_finished(self, worker: AvatarWorker):
        if worker in self._avatar_workers:
            self._avatar_workers.remove(worker)
        worker.deleteLater()

    def show_add_account_dialog(self):
        if self.login_task is not None or self.offline_task is not None:
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Add Account")
        dialog.setText("Choose how you want to add a Minecraft account.")
        dialog.setInformativeText("Offline accounts will need to be connected to a Wibsverse account for authentication.")
        microsoft = dialog.addButton("Microsoft account", QMessageBox.ButtonRole.AcceptRole)
        offline = dialog.addButton("Offline account", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        if dialog.clickedButton() is microsoft:
            self.add_microsoft()
        elif dialog.clickedButton() is offline:
            self.add_offline()

    def add_microsoft(self):
        if self.login_task is not None or self.offline_task is not None:
            return
        dialog = AccountHelpDialog(self)
        self.login_dialog = dialog
        self.login_task = LoginWorker(self.hmc_exe, self.root)
        self.login_task.device_url.connect(dialog.show_device_url)
        self.login_task.device_url.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))
        self.login_task.output.connect(dialog.show_output)
        self.login_task.progressed.connect(dialog.status.setText)
        self.login_task.executable_ready.connect(self._hmc_executable_ready)
        self.login_task.succeeded.connect(self._login_succeeded)
        self.login_task.failed.connect(self._login_failed)
        self.login_task.finished.connect(self._login_finished)
        self._set_busy(True)
        self.login_task.start()
        dialog.exec()

    def add_offline(self):
        if self.login_task is not None or self.offline_task is not None:
            return
        dialog = AddOfflineAccountDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name = dialog.minecraft_name.text().strip()
        email = dialog.email.text().strip()
        password = dialog.password.text()
        if not name or not email or not password:
            QMessageBox.warning(self, "Missing account details", "Enter a Minecraft username, WibsVerse email, and password.")
            return
        self.account_dialog = dialog
        self.offline_task = OfflineLoginWorker(self.hmc_exe, self.root, name, email, password)
        self.offline_task.progressed.connect(dialog.status.setText)
        self.offline_task.executable_ready.connect(self._hmc_executable_ready)
        self.offline_task.succeeded.connect(self._offline_login_succeeded)
        self.offline_task.failed.connect(self._offline_login_failed)
        self.offline_task.finished.connect(self._offline_login_finished)
        self._set_busy(True)
        self.offline_task.start()
        dialog.show()

    def _offline_login_succeeded(self, result: object):
        if isinstance(result, dict) and isinstance(result.get("account"), dict):
            account = result["account"]
            key = account_credential_key({"mode": "offline", "uuid": account["uuid"]})
            self._account_profiles()[key] = {
                "email": result.get("email", ""),
                "credentials_saved": True,
                "minecraft_linked": bool(result.get("minecraft_linked")),
            }
        self._refresh_accounts()
        self.status.setText("WibsVerse-connected offline account added.")
        if hasattr(self, "account_dialog"):
            self.account_dialog.accept()

    def _offline_login_failed(self, message: str):
        if hasattr(self, "account_dialog"):
            self.account_dialog.status.setText(message)
        QMessageBox.warning(self, "Offline account failed", message)

    def _offline_login_finished(self):
        self.offline_task = None
        self._set_busy(False)

    def remove_account(self):
        if self.login_task is not None or self.offline_task is not None or self.remove_account_task is not None:
            return
        available = self._available_accounts()
        if not available:
            return
        names = list(dict.fromkeys(account["name"] for account in available))
        name, accepted = QInputDialog.getItem(
            self, "Remove Account", "Choose the account to remove:", names, 0, False
        )
        if not accepted or not name:
            return
        answer = QMessageBox.question(
            self, "Remove Account", f"Remove {name} from this launcher?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.pending_removed_accounts = [entry for entry in available if entry["name"] == name]
        self.remove_account_task = RemoveAccountWorker(self.hmc_exe, self.root, name)
        self.remove_account_task.executable_ready.connect(self._hmc_executable_ready)
        self.remove_account_task.succeeded.connect(self._account_removed)
        self.remove_account_task.failed.connect(self._account_remove_failed)
        self.remove_account_task.finished.connect(self._account_remove_finished)
        self._set_busy(True)
        self.status.setText(f"Removing account {name}...")
        self.remove_account_task.start()

    def _account_removed(self):
        for account in getattr(self, "pending_removed_accounts", []):
            key = account_credential_key(account)
            delete_password(key)
            self._account_profiles().pop(key, None)
        self.pending_removed_accounts = []
        self.selection = {}
        self._refresh_accounts()
        self.status.setText("Account removed.")

    def _account_remove_failed(self, message: str):
        self.status.setText("Account removal failed: " + message)
        QMessageBox.warning(self, "Account removal failed", message)

    def _account_remove_finished(self):
        self.remove_account_task = None
        self._set_busy(False)

    def _hmc_executable_ready(self, executable: str):
        self.hmc_exe = executable
        self._persist()

    def _login_succeeded(self, accounts: object):
        self._refresh_accounts()
        self.status.setText("Microsoft account added. Select it from the account menu.")
        if hasattr(self, "login_dialog"):
            self.login_dialog.accept()

    def _login_failed(self, message: str):
        if hasattr(self, "login_dialog"):
            self.login_dialog.status.setText(message)
        QMessageBox.warning(self, "Microsoft login failed", message)

    def _login_finished(self):
        self.login_task = None
        self._set_busy(False)

    def _set_busy(self, busy: bool):
        installed = bool(read_installed(self.root).get("version"))
        active = busy or self.login_task is not None or self.offline_task is not None \
            or self.remove_account_task is not None or self.wibsverse_task is not None or self.launch_task is not None
        self.play_button.setEnabled(
            self._selected_game == "demon_slayer" and not active and installed and self._has_accounts
        )
        self.add_account_button.setEnabled(not active)
        self.settings_button.setEnabled(not active)

    def update_installed_label(self):
        version = read_installed(self.root).get("version", "Not installed")
        self.installed_label.setText("Wib’s Demon Slayer")
        if version == "Not installed":
            self.version_detail.setText("Modpack not installed yet")
        else:
            self.version_detail.setText(f"Modpack {version}  ·  Latest version installed")
        self._set_busy(self.task is not None)

    def _worker(self, operation: str, release: Release | None = None):
        if self.task is not None:
            return
        if operation == "install":
            self.update_dialog = UpdateDialog(self)
            self.update_dialog.show()
        self.task = UpdateWorker(operation, self.root, release)
        self.task.progressed.connect(self._progress)
        self.task.succeeded.connect(lambda result: self._completed(operation, result))
        self.task.failed.connect(lambda msg: self._failed(operation, msg))
        self.task.finished.connect(self._worker_finished)
        self._set_busy(True)
        self.task.start()

    def _progress(self, message: str, percentage: int):
        self.status.setText(message)
        if self.update_dialog:
            self.update_dialog.update_progress(message, percentage)

    def check_updates(self):
        if self.task:
            return
        if self.game_process is not None and self.game_process.poll() is None:
            self.status.setText("Close Minecraft before checking for updates.")
            return
        self.status.setText("Checking for modpack updates...")
        self._worker("check")

    def check_launcher_updates(self):
        if self.launcher_release_task is not None or self.launcher_download_task is not None:
            return
        self.launcher_release_task = LauncherReleaseWorker()
        self.launcher_release_task.succeeded.connect(self._launcher_release_checked)
        self.launcher_release_task.failed.connect(self._launcher_release_failed)
        self.launcher_release_task.finished.connect(self._launcher_release_finished)
        self.launcher_release_task.start()

    def _launcher_release_checked(self, result: object):
        if not isinstance(result, LauncherRelease):
            return
        self.launcher_release = result
        current = tuple(int(part) for part in LAUNCHER_VERSION.split("."))
        latest = tuple(int(part) for part in result.version.split("."))
        if latest <= current:
            return
        if self._launcher_update_prompted:
            return
        self._launcher_update_prompted = True
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Launcher update available")
        dialog.setText(f"Wibsverse Launcher {result.version} is available.")
        dialog.setInformativeText(f"You are running version {LAUNCHER_VERSION}. Update now to get the latest launcher.")
        update = dialog.addButton("Update Launcher", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        if dialog.clickedButton() is update:
            self._update_launcher(result)

    def _launcher_release_failed(self, message: str):
        self.status.setText("Launcher update check failed: " + message)

    def _launcher_release_finished(self):
        self.launcher_release_task = None

    def _update_launcher(self, release: LauncherRelease):
        if not release.asset_url:
            QDesktopServices.openUrl(QUrl(release.page_url))
            self.status.setText("Opening the launcher release page. Download the compatible installer there.")
            return
        self.status.setText(f"Downloading Wibsverse Launcher {release.version}...")
        self.launcher_download_task = LauncherUpdateWorker(release)
        self.launcher_download_task.succeeded.connect(self._launcher_downloaded)
        self.launcher_download_task.failed.connect(self._launcher_download_failed)
        self.launcher_download_task.finished.connect(self._launcher_download_finished)
        self.launcher_download_task.start()

    def _launcher_downloaded(self, path: object):
        if not isinstance(path, Path):
            self._launcher_download_failed("The downloaded launcher file was invalid")
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as error:
            self._launcher_download_failed(f"Could not open the launcher update: {error}")
            return
        self.launcher_download_task = None
        self.status.setText("Launcher update opened. Closing this launcher...")
        application = QApplication.instance()
        if application is not None:
            QTimer.singleShot(250, application.quit)

    def _launcher_download_failed(self, message: str):
        self.status.setText("Launcher update failed: " + message)
        QMessageBox.warning(self, "Launcher update failed", message)

    def _launcher_download_finished(self):
        self.launcher_download_task = None

    def _completed(self, operation: str, result: object):
        if operation == "check":
            if not isinstance(result, Release):
                self._failed(operation, "Unexpected release data")
                return
            self.release = result
            installed = read_installed(self.root)
            if installed.get("version") == result.version and installed.get("url") == result.url:
                self.status.setText(f"You're up to date — Wibsverse {result.version}")
            else:
                self.queued_release = result
                self.status.setText(f"New modpack version available: {result.version}")
        else:
            version = read_installed(self.root).get("version", "?")
            self.status.setText(f"Wibsverse {version} is installed and ready to play.")
            self._finish_update_dialog(True)

    def _finish_update_dialog(self, successful: bool):
        if self.update_dialog:
            dialog = self.update_dialog
            self.update_dialog = None
            dialog.active = False
            dialog.accept() if successful else dialog.reject()
            dialog.deleteLater()

    def _failed(self, operation: str, error: str):
        self.queued_release = None
        installed = read_installed(self.root)
        self.status.setText(("Update check failed: " if operation == "check" else "Update failed: ") + error)
        self._finish_update_dialog(False)
        # Existing modpack remains available; don't falsely report 100% on error.
        if operation != "check":
            QMessageBox.warning(self, "Modpack update failed", error +
                                ("\n\nYour previous installation is still available." if installed.get("version") else ""))

    def _worker_finished(self):
        self.task = None
        self.update_installed_label()
        if self.queued_release:
            release = self.queued_release
            self.queued_release = None
            QTimer.singleShot(0, lambda: self._worker("install", release))

    def select_hmc(self):
        file, _ = QFileDialog.getOpenFileName(self, "Select HeadlessMc executable", "", "All files (*)")
        if file:
            self.hmc_exe = file
            self._refresh_accounts()
            self._persist()

    def _check_hmc_path(self) -> bool:
        if not self.hmc_exe or not Path(self.hmc_exe).is_file():
            QMessageBox.warning(
                self, "HeadlessMc not found",
                "Install HeadlessMc and select its executable via Settings → Set HeadlessMc Path.\n"
                "https://headlesshq.github.io/headlessmc/",
            )
            return False
        return True

    def open_folder(self):
        folder = game_dir(self._game_root())
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_hmc_folder(self):
        self.root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.root)))

    def play(self):
        if self.task is not None:
            return
        if self.game_process is not None and self.game_process.poll() is None:
            QMessageBox.information(self, "Minecraft is already running", "Close the current game before launching another account.")
            return
        if not read_installed(self.root).get("version"):
            QMessageBox.warning(self, "Not installed", "The modpack must be installed before playing.")
            return
        name = self.selection.get("name")
        if self.selection.get("mode") not in {"microsoft", "offline"} or not name:
            self.account_button.showMenu()
            QMessageBox.information(self, "Select account", "Add or select an account in the top-right corner before playing.")
            return
        if self.launch_task is not None:
            return
        account = dict(self.selection)
        wibsverse_email = ""
        wibsverse_password = ""
        if account.get("mode") == "offline":
            profile = self._account_profile(account)
            wibsverse_email = str(profile.get("email") or "")
            try:
                wibsverse_password = load_password(account_credential_key(account)) or ""
            except WibsVerseAuthError as error:
                QMessageBox.warning(self, "WibsVerse account required", str(error))
                return
            if not wibsverse_email or not wibsverse_password:
                QMessageBox.information(
                    self, "Connect a WibsVerse account",
                    "Offline accounts need a WibsVerse account. Add its sign-in in Account menu → Account settings.",
                )
                self.edit_account_settings()
                return
        self._persist()
        self.launch_task = LaunchWorker(
            self.hmc_exe, self.root, name, account["mode"], account.get("uuid", ""),
            self.server_checkbox.isChecked(), wibsverse_email, wibsverse_password,
        )
        self.launch_task.progressed.connect(self.status.setText)
        self.launch_task.executable_ready.connect(self._hmc_executable_ready)
        self.launch_task.succeeded.connect(self._launch_succeeded)
        self.launch_task.failed.connect(self._launch_failed)
        self.launch_task.finished.connect(self._launch_finished)
        self._set_busy(True)
        self.status.setText("Preparing HeadlessMc...")
        self.launch_task.start()

    def _launch_succeeded(self, process: object):
        result = process if isinstance(process, dict) else {}
        game_process = result.get("process") if result else process
        if not isinstance(game_process, subprocess.Popen):
            self._launch_failed("HeadlessMc returned an invalid Minecraft process handle.")
            return
        self.game_process = game_process
        self._prepare_console_for_launch()
        if result.get("minecraft_linked"):
            key = str(result.get("account_key") or account_credential_key(self.selection))
            profile = dict(self._account_profiles().get(key) or {})
            profile["minecraft_linked"] = True
            profile["linked_minecraft_username"] = result.get("account_name", self.selection.get("name", ""))
            self._account_profiles()[key] = profile
            self._persist()
        handoff = result.get("sso_handoff")
        if isinstance(handoff, SsoHandoff):
            self.pending_link_handoff = handoff
            self.pending_link_account_key = str(result.get("account_key") or account_credential_key(self.selection))
            self.link_watch_timer.start()
        icon_file = game_icon_file()
        if icon_file is not None:
            self.game_icon_task = GameIconWorker(game_process.pid, icon_file)
            self.game_icon_task.finished.connect(self._game_icon_finished)
            self.game_icon_task.start()
        self.status.setText("Minecraft is starting through HeadlessMc. First launch may download Forge and libraries.")

    def _game_icon_finished(self):
        self.game_icon_task = None

    def _launch_failed(self, message: str):
        self.status.setText("Launch failed: " + message)
        QMessageBox.warning(self, "Cannot launch", message + "\n\nSee headlessmc-launch.log for details.")

    def _launch_finished(self):
        self.launch_task = None
        self._set_busy(False)

    def closeEvent(self, event):
        if (self.task is not None or self.migration_task is not None
                or self.login_task is not None or self.offline_task is not None
                or self.remove_account_task is not None or self.wibsverse_task is not None or self.launch_task is not None
                or self.launcher_release_task is not None or self.launcher_download_task is not None):
            event.ignore()
            QMessageBox.information(self, "Updating", "Please keep the launcher open until the current operation finishes.")
            return
        self._persist()
        self.console_timer.stop()
        self.link_watch_timer.stop()
        # Public-skin HTTP workers must finish before QApplication tears down Qt objects.
        for worker in list(self._avatar_workers):
            if worker.isRunning():
                worker.wait(22000)
        if self.game_icon_task is not None and self.game_icon_task.isRunning():
            self.game_icon_task.wait(50000)
        super().closeEvent(event)
        application = QApplication.instance()
        if application is not None:
            QTimer.singleShot(0, application.quit)

    def _account_profiles(self) -> dict:
        profiles = self.settings.get("account_profiles")
        if not isinstance(profiles, dict):
            profiles = {}
            self.settings["account_profiles"] = profiles
        return profiles

    def _account_profile(self, account: dict | None = None) -> dict:
        account = account or self.selection
        try:
            key = account_credential_key(account)
        except LauncherError:
            return {}
        profile = self._account_profiles().get(key)
        return profile if isinstance(profile, dict) else {}

    def edit_account_settings(self):
        account = dict(self.selection)
        if account.get("mode") not in {"offline", "microsoft"}:
            QMessageBox.information(self, "Account settings", "Choose a Minecraft account first.")
            return
        if self.wibsverse_task is not None:
            return
        key = account_credential_key(account)
        profile = self._account_profile(account)
        email, accepted = QInputDialog.getText(
            self, "WibsVerse Account", "WibsVerse email:", text=str(profile.get("email") or "")
        )
        if not accepted:
            return
        email = email.strip()
        if not email:
            if account["mode"] == "offline":
                QMessageBox.warning(self, "WibsVerse account required", "Offline accounts must have a WibsVerse account.")
                return
            answer = QMessageBox.question(
                self, "Unlink WibsVerse Account", "Remove the optional WibsVerse login from this Microsoft account?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                delete_password(key)
                self._account_profiles().pop(key, None)
                self._persist()
                self.status.setText("WibsVerse account unlinked.")
            return
        password, accepted = QInputDialog.getText(
            self, "WibsVerse Account", "WibsVerse password (leave blank to keep the saved password):",
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        if not password:
            try:
                password = load_password(key) or ""
            except WibsVerseAuthError as error:
                QMessageBox.warning(self, "WibsVerse account", str(error))
                return
        if not password:
            QMessageBox.warning(self, "WibsVerse account", "Enter a WibsVerse password to link this account.")
            return
        self.wibsverse_task = WibsVerseCredentialWorker(key, account["name"], email, password)
        self.wibsverse_task.succeeded.connect(
            lambda saved_email, a=account: self._wibsverse_credentials_saved(a, saved_email)
        )
        self.wibsverse_task.failed.connect(self._wibsverse_credentials_failed)
        self.wibsverse_task.finished.connect(self._wibsverse_credentials_finished)
        self._set_busy(True)
        self.status.setText("Verifying WibsVerse account...")
        self.wibsverse_task.start()

    def _wibsverse_credentials_saved(self, account: dict, email: str):
        key = account_credential_key(account)
        profiles = self._account_profiles()
        profile = profiles.get(key)
        profile = dict(profile) if isinstance(profile, dict) else {}
        profile["email"] = email
        profile["credentials_saved"] = True
        profiles[key] = profile
        self._persist()
        self.status.setText("WibsVerse account saved for this Minecraft account.")

    def _wibsverse_credentials_failed(self, message: str):
        self.status.setText("WibsVerse sign-in failed.")
        QMessageBox.warning(self, "WibsVerse sign-in failed", message)

    def _wibsverse_credentials_finished(self):
        self.wibsverse_task = None
        self._set_busy(False)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Wibsverse Launcher")
    app.setQuitOnLastWindowClosed(True)
    window = Launcher()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
