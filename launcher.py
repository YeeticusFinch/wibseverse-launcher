"""Wibsverse Launcher: custom UI backed by HeadlessMc."""
from __future__ import annotations

import ctypes
import os
import re
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QThread, QUrl, QRect, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QPainter, QPixmap, QColor, QLinearGradient
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton,
    QToolButton, QVBoxLayout, QWidget, QCheckBox,
    QInputDialog,
)

from core import (
    FORGE_VERSION, MINECRAFT_VERSION, LauncherError, Release, app_dir, atomic_json, ensure_instance,
    fetch_latest_release, find_headlessmc, game_dir, hmc_accounts, install_release,
    hmc_environment, hmc_launch_command, hmc_login_command,
    hmc_profile_command, hmc_remove_account_command,
    hmc_profile_is_pinned, hmc_select_account_command, legacy_prism_data_dir,
    create_hmc_offline_account, ensure_java17, install_headlessmc, load_json,
    migrate_legacy_instance, read_installed, run_hmc,
)
from skinutils import skin_png_for_uuid

PINK = "#D9439F"
INK = "#100B12"
CREAM = "#FFF1DF"
MUTED = "#BAA6B4"
ASSET_DIR = Path(__file__).resolve().parent / "assets"


def launcher_icon_asset() -> Path:
    return ASSET_DIR / "wibsverse_icon.png"


def game_icon_asset() -> Path:
    return ASSET_DIR / "logo.png"


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
                self.progressed.emit("HeadlessMc was not found. Installing the official Windows release...")
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

    def __init__(self, executable: str, root: Path, name: str):
        super().__init__()
        self.executable = executable
        self.root = root
        self.name = name

    def run(self):
        try:
            ensure_instance(self.root)
            if not self.executable or not Path(self.executable).is_file():
                self.progressed.emit("HeadlessMc was not found. Installing the official Windows release...")
                self.executable = str(install_headlessmc(
                    self.root, lambda message, _percentage: self.progressed.emit(message)
                ))
                self.executable_ready.emit(self.executable)
            self.progressed.emit(f"Adding offline account {self.name}...")
            create_hmc_offline_account(self.root, self.name)
            accounts = hmc_accounts(self.executable, self.root)
            if not any(account["name"] == self.name and account["mode"] == "offline" for account in accounts):
                raise LauncherError("HeadlessMC did not create the offline account")
            self.succeeded.emit(accounts)
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


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

    def __init__(self, executable: str, root: Path, account_name: str, join_server: bool):
        super().__init__()
        self.executable = executable
        self.root = root
        self.account_name = account_name
        self.join_server = join_server

    def run(self):
        try:
            ensure_instance(self.root)
            if not self.executable or not Path(self.executable).is_file():
                self.progressed.emit("HeadlessMc was not found. Installing the official Windows release...")
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
            log = log_path.open("ab")
            try:
                process = subprocess.Popen(
                    hmc_launch_command(self.executable, self.account_name, self.join_server),
                    cwd=str(self.root),
                    env=hmc_environment(self.root),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                log.close()
                raise
            log.close()
            self.succeeded.emit(process)
        except Exception as error:
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
        self.migration_task: MigrationWorker | None = None
        self.login_task: LoginWorker | None = None
        self.offline_task: OfflineLoginWorker | None = None
        self.remove_account_task: RemoveAccountWorker | None = None
        self.launch_task: LaunchWorker | None = None
        self.game_process: subprocess.Popen | None = None
        self.update_dialog: UpdateDialog | None = None
        self.queued_release: Release | None = None
        self.game_icon_task: GameIconWorker | None = None
        self._has_accounts = False
        self._avatar_icon = anonymous_avatar()
        self._avatar_uuid = ""
        self._avatar_workers: list[AvatarWorker] = []
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
        ''')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 0, 1, 1)
        outer.setSpacing(0)

        titlebar = QFrame()
        titlebar.setObjectName("titleBar")
        titlebar.setFixedHeight(32)
        title_layout = QHBoxLayout(titlebar)
        title_layout.setContentsMargins(18, 0, 10, 0)
        window_name = QLabel("WIBSVERSE LAUNCHER")
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
        game_tab = QFrame()
        game_tab.setStyleSheet("border-bottom:2px solid #ED4194;")
        game_tab_layout = QHBoxLayout(game_tab)
        game_tab_layout.setContentsMargins(12, 0, 12, 0)
        game_tab_layout.setSpacing(10)
        sword = QLabel()
        sword.setPixmap(QIcon(str(ASSET_DIR / "swords.svg")).pixmap(17, 17))
        game_tab_layout.addWidget(sword)
        game_name = QLabel("Demon Slayer")
        game_name.setStyleSheet("color:#FFF1DF; font-weight:600;")
        game_tab_layout.addWidget(game_name)
        game_tab_layout.addStretch()
        toolbar.addWidget(game_tab)
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
            ("Check for Updates", self.check_updates),
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
        header = QLabel("Wib’s Demon Slayer")
        header.setObjectName("heroTitle")
        intro.addWidget(header)
        description = QLabel("Walk the Slayer path or embrace the Demon path. Master techniques, conquer dungeons and rise through the ranks.")
        description.setObjectName("description")
        description.setFixedWidth(425)
        description.setWordWrap(True)
        intro.addWidget(description)
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
        outer.addWidget(showcase, 1)

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
        outer.addWidget(panel)

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
        self.play_button.setVisible(self._has_accounts)
        self.add_account_button.setVisible(not self._has_accounts)

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
        name, accepted = QInputDialog.getText(
            self, "Add Offline Account", "Enter the offline player name:"
        )
        name = name.strip()
        if not accepted or not name:
            return
        dialog = AccountHelpDialog(
            self,
            "Add Offline Account",
            "HeadlessMc will create this tokenless account using the player name you entered.",
        )
        self.account_dialog = dialog
        self.offline_task = OfflineLoginWorker(self.hmc_exe, self.root, name)
        self.offline_task.progressed.connect(dialog.status.setText)
        self.offline_task.executable_ready.connect(self._hmc_executable_ready)
        self.offline_task.succeeded.connect(self._offline_login_succeeded)
        self.offline_task.failed.connect(self._offline_login_failed)
        self.offline_task.finished.connect(self._offline_login_finished)
        self._set_busy(True)
        self.offline_task.start()
        dialog.exec()

    def _offline_login_succeeded(self, _accounts: object):
        self._refresh_accounts()
        self.status.setText("Offline account added. Select it from the account menu.")
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
        self.remove_account_task = RemoveAccountWorker(self.hmc_exe, self.root, name)
        self.remove_account_task.executable_ready.connect(self._hmc_executable_ready)
        self.remove_account_task.succeeded.connect(self._account_removed)
        self.remove_account_task.failed.connect(self._account_remove_failed)
        self.remove_account_task.finished.connect(self._account_remove_finished)
        self._set_busy(True)
        self.status.setText(f"Removing account {name}...")
        self.remove_account_task.start()

    def _account_removed(self):
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
            or self.remove_account_task is not None or self.launch_task is not None
        self.play_button.setEnabled(not active and installed and self._has_accounts)
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
        file, _ = QFileDialog.getOpenFileName(self, "Select HeadlessMc executable", "", "Executables (*.exe);;All files (*)")
        if file:
            self.hmc_exe = file
            self._refresh_accounts()
            self._persist()

    def _check_hmc_path(self) -> bool:
        if not self.hmc_exe or not Path(self.hmc_exe).is_file():
            QMessageBox.warning(
                self, "HeadlessMc not found",
                "Install HeadlessMc and select headlessmc.exe via Settings → Set HeadlessMc Path.\n"
                "https://headlesshq.github.io/headlessmc/",
            )
            return False
        return True

    def open_folder(self):
        folder = game_dir(self.root)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_hmc_folder(self):
        self.root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.root)))

    def play(self):
        if self.task is not None:
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
        self._persist()
        self.launch_task = LaunchWorker(
            self.hmc_exe, self.root, name, self.server_checkbox.isChecked()
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
        self.game_process = process
        icon_file = game_icon_file()
        if icon_file is not None and isinstance(process, subprocess.Popen):
            self.game_icon_task = GameIconWorker(process.pid, icon_file)
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
                or self.remove_account_task is not None or self.launch_task is not None):
            event.ignore()
            QMessageBox.information(self, "Updating", "Please keep the launcher open until the current operation finishes.")
            return
        self._persist()
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


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Wibsverse Launcher")
    app.setQuitOnLastWindowClosed(True)
    window = Launcher()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
