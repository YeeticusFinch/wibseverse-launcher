"""Wibsverse Launcher 0.2: polished Windows-first Prism wrapper UI."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QPainter, QPixmap, QColor
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton,
    QToolButton, QVBoxLayout, QWidget, QCheckBox,
)

from core import (
    INSTANCE_ID, LauncherError, Release, app_dir, atomic_json, ensure_instance,
    fetch_latest_release, find_prism, game_dir, install_release, instance_dir,
    legacy_prism_data_dir, load_json, migrate_legacy_instance, prism_accounts,
    prism_launch_command, read_installed, standard_prism_data_dir, validate_offline_name,
)
from skinutils import skin_png_for_uuid

PINK = "#D9439F"


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
    skin = QImage.fromData(data, b"PNG")
    if skin.isNull() or skin.width() < 64 or skin.height() < 32 or skin.width() % 64:
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
    """Explains the Prism-owned login; never asks for passwords or callback tokens."""
    def __init__(self, parent: "Launcher"):
        super().__init__(parent)
        self.setWindowTitle("Add Microsoft Account")
        self.setMinimumWidth(460)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 25, 28, 25)
        layout.setSpacing(14)
        title = QLabel("Sign in with Microsoft")
        title.setObjectName("updateTitle")
        layout.addWidget(title)
        info = QLabel(
            "Wibsverse uses your existing Prism accounts. Open Prism's account manager, "
            "choose Add Microsoft, and sign in there.\n\n"
            "For a login that doesn't reopen Prism from the browser, use the "
            "one-time code shown in Prism: go to microsoft.com/link and enter the code. "
            "Prism completes the sign-in automatically.\n\n"
            "Your new player profile will then appear in Wibsverse."
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        button = QPushButton("Open Prism to add account")
        button.setObjectName("primary")
        button.clicked.connect(lambda: (parent.open_prism(), self.accept()))
        layout.addWidget(button)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel)


class Launcher(QWidget):
    def __init__(self):
        super().__init__()
        app_dir().mkdir(parents=True, exist_ok=True)
        saved = load_json(app_dir() / "settings.json", {})
        self.settings: dict = saved if isinstance(saved, dict) else {}
        self._new_root_selection = "prism_root" not in self.settings
        self.prism_exe = str(self.settings.get("prism_exe") or find_prism() or "")
        self.root = Path(self.settings.get("prism_root") or standard_prism_data_dir())
        self.selection: dict = self.settings.get("selected_account", {})
        if not isinstance(self.selection, dict):
            self.selection = {}
        self.release: Release | None = None
        self.task: UpdateWorker | None = None
        self.migration_task: MigrationWorker | None = None
        self.game_process: subprocess.Popen | None = None
        self.update_dialog: UpdateDialog | None = None
        self.queued_release: Release | None = None
        self._avatar_icon = anonymous_avatar()
        self._avatar_uuid = ""
        self._avatar_workers: list[AvatarWorker] = []
        self._accounts_signature: tuple | None = None
        self.setWindowTitle("Wibsverse Launcher")
        self.resize(920, 615)
        self.setMinimumSize(690, 500)
        self._ui()
        self._refresh_accounts()
        self.update_installed_label()
        self.accounts_timer = QTimer(self)
        self.accounts_timer.setInterval(1800)
        self.accounts_timer.timeout.connect(self._poll_accounts)
        self.accounts_timer.start()
        QTimer.singleShot(180, self._startup)

    def _ui(self):
        self.setStyleSheet('''
          QWidget { background:#101119; color:#F4EFF7; font: 11pt "Segoe UI"; }
          QLabel#wordmark { color:#F3B8DC; font-size:31pt; font-weight:900; }
          QLabel#heroTitle { color:#FFFFFF; font-size:23pt; font-weight:750; }
          QLabel#sub { color:#B8B2C6; font-size:11pt; }
          QLabel#muted { color:#A6A3B7; font-size:9pt; }
          QLabel#version { color:#ECD1E2; font-size:10pt; }
          QLabel#status { color:#CCC6D6; font-size:10pt; }
          QLabel#updateTitle { font-size:18pt; font-weight:750; }
          QFrame#hero { background:#1A1B29; border:1px solid #353044; border-radius:18px; }
          QFrame#smallLine { background:#463C55; min-height:1px; max-height:1px; }
          QPushButton, QToolButton { background:#2E2C3C; border:1px solid #494254;
              border-radius:10px; padding:11px 17px; }
          QPushButton:hover:enabled, QToolButton:hover { background:#40374C; }
          QPushButton#play { background:#BB267E; border:1px solid #D9439F;
              font-size:19pt; font-weight:800; border-radius:13px; padding:22px; }
          QPushButton#play:hover:enabled { background:#CF318F; }
          QPushButton#play:disabled { background:#653451; color:#B5A9B1; }
          QPushButton#primary { background:#BA277E; font-weight:650; }
          QToolButton#account { font-size:11pt; font-weight:600; padding:6px 12px;
              background:#21202F; border-radius:11px; }
          QToolButton#settings { font-size:17pt; padding:6px 12px; }
          QMenu { background:#222232; border:1px solid #504658; padding:7px; }
          QMenu::item { padding:10px 24px; border-radius:6px; }
          QMenu::item:selected { background:#6E315A; }
          QMenu::separator { height:1px; background:#54495B; margin:6px 8px; }
          QProgressBar { background:#37303F; border:0; border-radius:6px; height:17px; text-align:center; }
          QProgressBar::chunk { background:#D9439F; border-radius:6px; }
          QDialog { background:#1A1B27; }
          QLineEdit { background:#2A2936; border:1px solid #54495B; border-radius:8px; padding:12px; }
        ''')
        outer = QVBoxLayout(self)
        outer.setContentsMargins(34, 22, 34, 28)
        outer.setSpacing(18)

        toolbar = QHBoxLayout()
        logo = QLabel("WIBSVERSE")
        logo.setObjectName("wordmark")
        toolbar.addWidget(logo)
        toolbar.addStretch()
        self.account_button = QToolButton()
        self.account_button.setObjectName("account")
        self.account_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.account_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.account_button.setIconSize(QPixmap(36, 36).size())
        self.account_menu = QMenu(self)
        self.account_button.setMenu(self.account_menu)
        toolbar.addWidget(self.account_button)
        self.settings_button = QToolButton()
        self.settings_button.setObjectName("settings")
        self.settings_button.setText("⚙")
        self.settings_button.setToolTip("Settings")
        self.settings_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.settings_menu = QMenu(self)
        for label, callback in (
            ("Check for Updates", self.check_updates),
            ("Set Prism Path...", self.select_prism),
            ("Game Files", self.open_folder),
            ("Open Prism Launcher", self.open_prism),
        ):
            self.settings_menu.addAction(label).triggered.connect(callback)
        self.settings_menu.addSeparator()
        self.settings_menu.addAction("Prism Data Folder").triggered.connect(self.open_prism_folder)
        self.settings_menu.addAction("Switch Prism Data Folder...").triggered.connect(self.choose_prism_root)
        self.settings_button.setMenu(self.settings_menu)
        toolbar.addWidget(self.settings_button)
        outer.addLayout(toolbar)

        outer.addStretch(1)
        hero = QFrame()
        hero.setObjectName("hero")
        hv = QVBoxLayout(hero)
        hv.setContentsMargins(30, 34, 30, 32)
        hv.setSpacing(20)
        header = QLabel("Wib's Demon Slayer")
        header.setObjectName("heroTitle")
        hv.addWidget(header)
        subtitle = QLabel("MINECRAFT 1.20.1    •    FORGE 47.4.23")
        subtitle.setObjectName("sub")
        hv.addWidget(subtitle)
        line = QFrame()
        line.setObjectName("smallLine")
        hv.addWidget(line)
        self.installed_label = QLabel("Checking installation...")
        self.installed_label.setObjectName("version")
        hv.addWidget(self.installed_label)
        self.status = QLabel("Checking for updates...")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        hv.addWidget(self.status)
        hv.addSpacing(8)
        self.play_button = QPushButton("▶   PLAY WIBSVERSE")
        self.play_button.setObjectName("play")
        self.play_button.clicked.connect(self.play)
        hv.addWidget(self.play_button)
        outer.addWidget(hero)
        outer.addStretch(1)
        footer = QHBoxLayout()
        footer.addWidget(QLabel("Powered by Prism Launcher"))
        footer.addStretch()
        self.server_checkbox = QCheckBox("Join Wibsverse server automatically")
        self.server_checkbox.setChecked(bool(self.settings.get("join_server", True)))
        self.server_checkbox.toggled.connect(lambda _: self._persist())
        footer.addWidget(self.server_checkbox)
        outer.addLayout(footer)

    def _startup(self):
        old_root = legacy_prism_data_dir()
        target = standard_prism_data_dir()
        # Keep saves from the original isolated launcher; never copy accounts.json.
        if (self._new_root_selection and instance_dir(old_root).exists()
                and not instance_dir(target).exists() and old_root != target):
            answer = QMessageBox.question(
                self, "Keep your Wibsverse worlds",
                "The previous Wibsverse launcher used a separate Prism folder.\n\n"
                "Copy your existing Wibsverse modpack and worlds into the regular Prism folder? "
                "This lets Wibsverse use your existing Minecraft accounts.\n\n"
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
            self.root = old_root
        self._after_startup()

    def _migration_done(self, target: Path):
        self.root = target
        self._finish_update_dialog(True)

    def _migration_failed(self, old_root: Path, message: str):
        self.root = old_root
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
        self.settings["prism_exe"] = self.prism_exe
        self.settings["prism_root"] = str(self.root)
        self.settings["selected_account"] = dict(self.selection)
        if hasattr(self, "server_checkbox"):
            self.settings["join_server"] = self.server_checkbox.isChecked()
        atomic_json(app_dir() / "settings.json", self.settings)

    def _poll_accounts(self):
        path = self.root / "accounts.json"
        try:
            st = path.stat()
            signature = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            signature = (str(path), None, None)
        if signature != self._accounts_signature:
            self._refresh_accounts()

    def _available_accounts(self) -> list[dict]:
        try:
            prism = prism_accounts(self.root)
        except (OSError, ValueError):
            prism = []
        result: list[dict] = []
        for a in prism:
            category = "microsoft" if a["type"].upper() in ("MSA", "MICROSOFT") else "offline"
            result.append({"mode": category, "name": a["name"], "uuid": a.get("uuid", "")})
        saved_offline_names = self.settings.get("offline_names", [])
        if not isinstance(saved_offline_names, list):
            saved_offline_names = []
        for name in saved_offline_names:
            if isinstance(name, str) and validate_offline_name(name) and not any(a["mode"] == "offline" and a["name"] == name for a in result):
                result.append({"mode": "offline", "name": name, "uuid": ""})
        return result

    def _refresh_accounts(self):
        path = self.root / "accounts.json"
        try:
            st = path.stat()
            self._accounts_signature = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            self._accounts_signature = (str(path), None, None)
        available = self._available_accounts()
        if not any(a["mode"] == self.selection.get("mode") and a["name"] == self.selection.get("name") for a in available):
            self.selection = dict(next((a for a in available if a["mode"] == "microsoft"),
                                       available[0] if available else {}))
        else:
            self.selection = dict(next(a for a in available if a["mode"] == self.selection.get("mode")
                                       and a["name"] == self.selection.get("name")))
        self._rebuild_account_menu(available)
        self._display_selected_account()
        self._persist()

    def _rebuild_account_menu(self, available: list[dict]):
        menu = self.account_menu
        menu.clear()
        menu.addSection("Minecraft accounts")
        if not available:
            nothing = menu.addAction("No accounts added yet")
            nothing.setEnabled(False)
        for a in available:
            title = a["name"] if a["mode"] == "microsoft" else f"{a['name']}  (Offline)"
            act = menu.addAction(title)
            act.setCheckable(True)
            act.setChecked(a["mode"] == self.selection.get("mode") and a["name"] == self.selection.get("name"))
            act.triggered.connect(lambda checked=False, entry=dict(a): self._choose_account(entry))
        menu.addSeparator()
        menu.addAction("+  Add Microsoft account...").triggered.connect(self.add_microsoft)
        menu.addAction("+  Add offline username...").triggered.connect(self.add_offline)
        menu.addAction("Manage accounts in Prism...").triggered.connect(self.open_prism)
        menu.addAction("Refresh accounts").triggered.connect(self._refresh_accounts)

    def _choose_account(self, account: dict):
        self.selection = dict(account)
        self._display_selected_account()
        QTimer.singleShot(0, lambda: self._rebuild_account_menu(self._available_accounts()))
        self._persist()

    def _display_selected_account(self):
        name = self.selection.get("name", "")
        self.account_button.setText((name if name else "Accounts") + "  ▾")
        if hasattr(self, "server_checkbox"):
            self.server_checkbox.setEnabled(self.selection.get("mode") == "microsoft")
        uuid_text = self.selection.get("uuid", "") if self.selection.get("mode") == "microsoft" else ""
        if uuid_text != self._avatar_uuid:
            self._avatar_uuid = uuid_text
            self._avatar_icon = anonymous_avatar()
            if uuid_text:
                worker = AvatarWorker(uuid_text)
                self._avatar_workers.append(worker)
                worker.fetched.connect(self._avatar_fetched)
                worker.finished.connect(lambda w=worker: self._avatar_finished(w))
                worker.start()
        self.account_button.setIcon(self._avatar_icon)

    def _avatar_fetched(self, uuid_text: str, data: bytes):
        if uuid_text == self._avatar_uuid:
            result = avatar_from_skin(data)
            if result is not None:
                self._avatar_icon = result
                self.account_button.setIcon(result)

    def _avatar_finished(self, worker: AvatarWorker):
        if worker in self._avatar_workers:
            self._avatar_workers.remove(worker)
        worker.deleteLater()

    def add_microsoft(self):
        if not self._check_prism_path():
            return
        dialog = AccountHelpDialog(self)
        dialog.exec()

    def add_offline(self):
        name, ok = QInputDialog.getText(self, "Add Offline Account", "Minecraft offline username (3-16 letters, numbers, underscores):")
        if not ok:
            return
        name = name.strip()
        if not validate_offline_name(name):
            QMessageBox.warning(self, "Invalid username", "Use 3-16 characters: letters, numbers, or underscores.")
            return
        names = self.settings.get("offline_names", [])
        if not isinstance(names, list):
            names = []
        if name not in names:
            names.append(name)
        self.settings["offline_names"] = names
        self.selection = {"mode": "offline", "name": name, "uuid": ""}
        self._refresh_accounts()
        self.status.setText("Offline account selected. Offline usernames cannot join authenticated multiplayer servers.")

    def _set_busy(self, busy: bool):
        installed = bool(read_installed(self.root).get("version"))
        self.play_button.setEnabled(not busy and installed)
        self.settings_button.setEnabled(not busy)

    def update_installed_label(self):
        version = read_installed(self.root).get("version", "Not installed")
        self.installed_label.setText("Wibsverse Modpack  •  " + str(version))
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

    def select_prism(self):
        file, _ = QFileDialog.getOpenFileName(self, "Select Prism Launcher executable", "", "Executables (*.exe);;All files (*)")
        if file:
            self.prism_exe = file
            self._persist()

    def choose_prism_root(self):
        path = QFileDialog.getExistingDirectory(self, "Select Prism Launcher data folder", str(self.root))
        if path and Path(path) != self.root:
            self.root = Path(path)
            self._avatar_uuid = ""
            self._accounts_signature = None
            self._refresh_accounts()
            self.update_installed_label()
            self._persist()
            self.check_updates()

    def _check_prism_path(self) -> bool:
        if not self.prism_exe or not Path(self.prism_exe).is_file():
            QMessageBox.warning(self, "Prism not found", "Install Prism Launcher and select prismlauncher.exe via Settings → Set Prism Path.\nhttps://prismlauncher.org/download/")
            return False
        return True

    def open_prism(self):
        if not self._check_prism_path():
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([self.prism_exe, "-d", str(self.root)])
            self.status.setText("In Prism, choose Accounts → Manage Accounts → Add Microsoft. Wibsverse will detect the new account automatically.")
        except OSError as error:
            QMessageBox.critical(self, "Prism error", str(error))

    def open_folder(self):
        folder = game_dir(self.root)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_prism_folder(self):
        self.root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.root)))

    def play(self):
        if self.task is not None:
            return
        if not read_installed(self.root).get("version"):
            QMessageBox.warning(self, "Not installed", "The modpack must be installed before playing.")
            return
        if not self._check_prism_path():
            return
        mode, name = self.selection.get("mode"), self.selection.get("name")
        if not mode or not name:
            self.account_button.showMenu()
            QMessageBox.information(self, "Select account", "Add or select an account in the top-right corner before playing.")
            return
        try:
            ensure_instance(self.root)
            if mode == "microsoft":
                command = prism_launch_command(self.prism_exe, self.root, online_name=name,
                                              join_server=self.server_checkbox.isChecked())
            else:
                command = prism_launch_command(self.prism_exe, self.root, offline_name=name,
                                              join_server=False)
            self._persist()
            self.game_process = subprocess.Popen(command)
            self.status.setText("Starting Minecraft through Prism. First launch may download libraries and Forge.")
        except LauncherError as error:
            QMessageBox.warning(self, "Cannot launch", str(error))
        except OSError as error:
            QMessageBox.critical(self, "Prism error", str(error))

    def closeEvent(self, event):
        if self.task is not None or self.migration_task is not None:
            event.ignore()
            QMessageBox.information(self, "Updating", "Please keep the launcher open until the current operation finishes.")
            return
        self._persist()
        # Public-skin HTTP workers must finish before QApplication tears down Qt objects.
        for worker in list(self._avatar_workers):
            if worker.isRunning():
                worker.wait(22000)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Wibsverse Launcher")
    window = Launcher()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
