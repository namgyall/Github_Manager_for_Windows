from __future__ import annotations

import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QPointF, QRunnable, QSettings, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from credential_store import CredentialStore, CredentialStoreError
from github_api import (
    DeviceAuthorization,
    authenticate_github,
    GitHubAPI,
    GitHubError,
    Repository,
    RepositoryFile,
    scan_local_files,
)


APP_NAME = "Github Manager"
ORGANIZATION = "Namgyal"
STYLE = """
QWidget { color: #0c2b21; font-family: "Segoe UI"; font-size: 10pt; }
QMainWindow, QDialog { background: #e9f4ee; }
QFrame#panel {
    background: rgba(255,255,255,238);
    border: 1px solid #b9daca;
    border-radius: 12px;
}
QLabel#title { font-size: 21pt; font-weight: 700; color: #073b2c; }
QLabel#subtitle, QLabel#hint { color: #49675c; }
QLabel#section { font-size: 11pt; font-weight: 700; color: #123b2e; }
QPushButton {
    background: #146b48; color: white; border: 1px solid #0a5939;
    border-radius: 7px; padding: 8px 13px; font-weight: 600;
}
QPushButton:hover { background: #188154; }
QPushButton:pressed { background: #0c5136; }
QPushButton:disabled { background: #9ab5a7; color: #eef5f0; border-color: #9ab5a7; }
QPushButton#danger { background: #a93434; border-color: #862727; }
QPushButton#danger:hover { background: #c14444; }
QLineEdit, QComboBox, QListWidget, QPlainTextEdit {
    background: white; border: 1px solid #b6cfc1; border-radius: 6px;
    padding: 6px; selection-background-color: #b6ecd0;
}
QComboBox::drop-down { border: 0; width: 24px; }
QListWidget { padding: 4px; }
QListWidget::item {
    padding: 5px 6px;
    border-radius: 4px;
}
QListWidget::item:hover {
    background-color: #f0f7f3;
}
QListWidget::item:selected {
    background-color: #b6ecd0;
    color: #073b2c;
}
QProgressBar { border: 1px solid #b6cfc1; border-radius: 5px; background: white; height: 12px; }
QProgressBar::chunk { background: #28a66c; border-radius: 4px; }
QCheckBox {
    spacing: 9px;
    color: #0c2b21;
}
QComboBox QAbstractItemView {
    background: white;
    border: 1px solid #b6cfc1;
    border-radius: 6px;
    padding: 4px;
    selection-background-color: #b6ecd0;
}
QComboBox QAbstractItemView::item {
    padding: 6px 8px;
    min-height: 28px;
}
QComboBox QAbstractItemView::item:hover {
    background-color: #f0f7f3;
}
QComboBox QAbstractItemView::item:selected {
    background-color: #b6ecd0;
    color: #073b2c;
}
QCheckBox::indicator, QListWidget::indicator, QComboBox QAbstractItemView::indicator {
    width: 20px;
    height: 20px;
    border: 2px solid #0f5132;
    border-radius: 4px;
    background-color: #ffffff;
}
QCheckBox::indicator:hover, QListWidget::indicator:hover, QComboBox QAbstractItemView::indicator:hover {
    border-color: #0a3622;
    background-color: #e8f5e9;
}
QCheckBox::indicator:checked, QListWidget::indicator:checked, QComboBox QAbstractItemView::indicator:checked {
    border-color: #0a3622;
    background-color: #146b48;
    image: url("__CHECK_ICON__");
}
QCheckBox::indicator:checked:hover, QListWidget::indicator:checked:hover, QComboBox QAbstractItemView::indicator:checked:hover {
    border-color: #062b1a;
    background-color: #188154;
    image: url("__CHECK_ICON__");
}
QDialogButtonBox QPushButton { min-width: 90px; }
"""


def resource_path(name: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / "Resources" / name


def ensure_checkbox_icon() -> str:
    icon_path = resource_path("checkbox_check.png")
    if not icon_path.exists():
        try:
            icon_path.parent.mkdir(parents=True, exist_ok=True)
            img = QImage(32, 32, QImage.Format.Format_ARGB32)
            img.fill(Qt.GlobalColor.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor("#ffffff"), 3.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawLine(QPointF(7, 16), QPointF(13, 23))
            p.drawLine(QPointF(13, 23), QPointF(25, 9))
            p.end()
            img.save(str(icon_path))
        except Exception:
            pass
    return icon_path.as_posix()


def build_stylesheet() -> str:
    check_icon = ensure_checkbox_icon()
    return STYLE.replace("__CHECK_ICON__", check_icon)


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(object)
    progress = Signal(object)


class Worker(QRunnable):
    def __init__(self, function: Callable, *args, **kwargs):
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self):
        try:
            if not self.args and not self.kwargs:
                import inspect
                try:
                    sig = inspect.signature(self.function)
                    param_names = [p.name for p in sig.parameters.values()]
                    req_params = [
                        p for p in sig.parameters.values()
                        if p.default == inspect.Parameter.empty
                        and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    ]
                    if len(req_params) == 1 or "progress" in param_names:
                        result = self.function(self.signals.progress.emit)
                    else:
                        result = self.function()
                except (ValueError, TypeError):
                    try:
                        result = self.function(self.signals.progress.emit)
                    except TypeError:
                        result = self.function()
            else:
                result = self.function(*self.args, **self.kwargs)
        except Exception as error:
            self.signals.error.emit(error)
        else:
            self.signals.result.emit(result)


def create_worker(function: Callable, on_result, on_error, on_progress=None) -> Worker:
    worker = Worker(function)
    worker.signals.result.connect(on_result)
    worker.signals.error.connect(on_error)
    if on_progress is not None:
        worker.signals.progress.connect(on_progress)
    QThreadPool.globalInstance().start(worker)
    return worker


def make_panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    panel = QFrame()
    panel.setObjectName("panel")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(15, 13, 15, 15)
    layout.setSpacing(9)
    heading = QLabel(title)
    heading.setObjectName("section")
    layout.addWidget(heading)
    return panel, layout


class ConnectionDialog(QDialog):
    authorize_requested = Signal(str)
    cancel_requested = Signal()

    def __init__(self, client_id: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect GitHub")
        self.setMinimumWidth(480)
        self.client_id = QLineEdit(client_id)
        self.client_id.setPlaceholderText("OAuth App Client ID")
        self.client_id.setObjectName("clientId")
        self.code = QLineEdit()
        self.code.setReadOnly(True)
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setStyleSheet("font-size: 20pt; font-weight: 700; letter-spacing: 3px;")
        self.code.hide()
        self.copy_button = QPushButton("Copy one-time code")
        self.copy_button.hide()
        self.status = QLabel(
            "Enter the public Client ID of an OAuth App with Device Flow enabled. "
            "The app never asks for a client secret."
        )
        self.status.setWordWrap(True)
        self.error = QLabel()
        self.error.setStyleSheet("color: #a12525;")
        self.error.setWordWrap(True)
        self.authorize_button = QPushButton("Authorize with GitHub")
        cancel = QPushButton("Cancel")
        self.authorize_button.clicked.connect(
            lambda: self.authorize_requested.emit(self.client_id.text().strip())
        )
        self.copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(self.code.text())
        )
        cancel.clicked.connect(self._cancel)
        self._cancel_button = cancel

        layout = QVBoxLayout(self)
        intro = QLabel("GitHub Device Flow")
        intro.setObjectName("title")
        layout.addWidget(intro)
        layout.addWidget(self.status)
        layout.addWidget(self.client_id)
        layout.addWidget(self.code)
        layout.addWidget(self.copy_button)
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.authorize_button)
        layout.addLayout(buttons)
        self.authorize_button.setDefault(True)

    def set_waiting(self, waiting: bool):
        self.authorize_button.setEnabled(not waiting)
        self.authorize_button.setText(
            "Waiting for GitHub…" if waiting else "Authorize with GitHub"
        )
        self.client_id.setEnabled(not waiting)
        self._cancel_button.setText("Cancel authorization" if waiting else "Cancel")
        self.error.clear()

    def show_device_code(self, authorization: DeviceAuthorization):
        self.code.setText(authorization.user_code)
        self.code.show()
        self.copy_button.show()
        self.status.setText(
            f"Visit {authorization.verification_uri} and enter this one-time code. "
            "Authorization is checked securely in the background."
        )

    def show_progress(self, message: str):
        self.status.setText(message)

    def show_error(self, message: str):
        self.error.setText(message)

    def _cancel(self):
        self.cancel_requested.emit()
        self.reject()


class CreateRepositoryDialog(QDialog):
    def __init__(self, on_create: Callable, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create repository")
        self.setMinimumWidth(470)
        self.on_create = on_create
        self.name = QLineEdit()
        self.name.setPlaceholderText("my-new-repository")
        self.description = QLineEdit()
        self.description.setMaxLength(350)
        self.private = QCheckBox("Private repository")
        self.private.setChecked(True)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #a12525;")
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Description", self.description)
        form.addRow("", self.private)
        layout = QVBoxLayout(self)
        title = QLabel("Create GitHub repository")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addLayout(form)
        layout.addWidget(self.error)
        actions = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        actions.button(QDialogButtonBox.StandardButton.Ok).setText("Create private repository")
        actions.accepted.connect(self._submit)
        actions.rejected.connect(self.reject)
        self.private.toggled.connect(
            lambda checked: actions.button(QDialogButtonBox.StandardButton.Ok).setText(
                "Create private repository" if checked else "Create public repository"
            )
        )
        layout.addWidget(actions)
        self.actions = actions

    def _submit(self):
        name = self.name.text().strip()
        description = self.description.text().strip()
        if (
            not name
            or len(name) > 100
            or name in (".", "..")
            or name.lower().endswith(".git")
            or not re.fullmatch(r"[A-Za-z0-9._-]+", name)
        ):
            self.error.setText(
                "Use 1–100 letters, numbers, periods, hyphens, or underscores. "
                "Names cannot be . or .. or end in .git."
            )
            return
        self.error.clear()
        self.actions.setEnabled(False)
        self.name.setEnabled(False)
        self.description.setEnabled(False)
        self.on_create(name, description, self.private.isChecked(), self)

    def create_succeeded(self):
        self.accept()

    def create_failed(self, message: str):
        self.actions.setEnabled(True)
        self.name.setEnabled(True)
        self.description.setEnabled(True)
        self.error.setText(message)


class DeleteFilesDialog(QDialog):
    activity = Signal(str, bool)

    def __init__(
        self,
        api: GitHubAPI,
        token: str,
        repository: Repository,
        parent=None,
    ):
        super().__init__(parent)
        self.api = api
        self.token = token
        self.repository = repository
        self.branches: list[str] = []
        self.files: list[RepositoryFile] = []
        self.worker: Worker | None = None
        self.setWindowTitle("Delete repository files")
        self.resize(710, 610)
        layout = QVBoxLayout(self)
        title = QLabel("Review repository files")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addWidget(QLabel(repository.full_name))
        branch_row = QHBoxLayout()
        branch_row.addWidget(QLabel("Branch"))
        self.branch = QComboBox()
        self.branch.currentTextChanged.connect(self._load_files)
        branch_row.addWidget(self.branch, 1)
        self.refresh = QPushButton("Refresh")
        self.refresh.clicked.connect(self._refresh)
        branch_row.addWidget(self.refresh)
        layout.addLayout(branch_row)
        selection_row = QHBoxLayout()
        self.count = QLabel("0 selected")
        self.select_all = QPushButton("Select all")
        self.clear = QPushButton("Clear")
        self.select_all.clicked.connect(lambda: self._set_all(True))
        self.clear.clicked.connect(lambda: self._set_all(False))
        selection_row.addWidget(QLabel("Files on selected branch"))
        selection_row.addStretch(1)
        selection_row.addWidget(self.count)
        selection_row.addWidget(self.select_all)
        selection_row.addWidget(self.clear)
        layout.addLayout(selection_row)
        self.file_list = QListWidget()
        self.file_list.itemChanged.connect(self._update_count)
        layout.addWidget(self.file_list, 1)
        self.status = QLabel("Loading branches…")
        self.status.setWordWrap(True)
        self.status.setObjectName("hint")
        layout.addWidget(self.status)
        buttons = QDialogButtonBox()
        cancel = buttons.addButton(
            "Cancel", QDialogButtonBox.ButtonRole.RejectRole
        )
        self.delete_button = buttons.addButton(
            "Review deletion…", QDialogButtonBox.ButtonRole.DestructiveRole
        )
        self.delete_button.setObjectName("danger")
        cancel.clicked.connect(self.reject)
        self.delete_button.clicked.connect(self._review_deletion)
        layout.addWidget(buttons)
        self._load_branches()

    def _load_branches(self):
        self.status.setText("Loading branches…")
        self._start(
            lambda: self.api.list_branches(self.token, self.repository),
            self._branches_loaded,
        )

    def _branches_loaded(self, branches: list[str]):
        self.branches = branches
        self.branch.blockSignals(True)
        self.branch.clear()
        self.branch.addItems(branches)
        if self.repository.default_branch in branches:
            self.branch.setCurrentText(self.repository.default_branch)
        self.branch.blockSignals(False)
        if branches:
            self._load_files(self.branch.currentText())
        else:
            self.status.setText("No branches are available in this repository.")

    def _load_files(self, branch: str):
        if not branch:
            return
        self.file_list.clear()
        self.status.setText(f"Loading files from {branch}…")
        self._start(
            lambda: self.api.list_files(self.token, self.repository, branch),
            self._files_loaded,
        )

    def _files_loaded(self, files: list[RepositoryFile]):
        self.files = files
        self.file_list.blockSignals(True)
        for file in files:
            item = QListWidgetItem(file.path)
            item.setData(Qt.ItemDataRole.UserRole, file)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.file_list.addItem(item)
        self.file_list.blockSignals(False)
        self.status.setText(
            f"{len(files)} file(s) on {self.branch.currentText()}."
            if files
            else "This branch contains no files."
        )
        self._update_count()

    def _refresh(self):
        if not self.branches:
            self._load_branches()
        else:
            self._load_files(self.branch.currentText())

    def _set_all(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for index in range(self.file_list.count()):
            self.file_list.item(index).setCheckState(state)

    def _selected_files(self) -> list[RepositoryFile]:
        return [
            self.file_list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.file_list.count())
            if self.file_list.item(index).checkState() == Qt.CheckState.Checked
        ]

    def _update_count(self, *_):
        self.count.setText(f"{len(self._selected_files())} selected")
        self.delete_button.setEnabled(bool(self._selected_files()))

    def _review_deletion(self):
        selected = self._selected_files()
        if not selected:
            return
        review = QDialog(self)
        review.setWindowTitle("Confirm file deletion")
        review.resize(560, 480)
        layout = QVBoxLayout(review)
        warning = QLabel(
            f"Delete {len(selected)} file(s) from {self.repository.full_name}, "
            f"branch {self.branch.currentText()}?"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("font-weight: 700; color: #942525;")
        layout.addWidget(warning)
        layout.addWidget(
            QLabel("Check the exact paths below. GitHub creates one commit for each deleted file.")
        )
        paths = QListWidget()
        paths.addItems(file.path for file in selected)
        layout.addWidget(paths, 1)
        actions = QDialogButtonBox()
        back = actions.addButton("Back to selection", QDialogButtonBox.ButtonRole.RejectRole)
        confirm = actions.addButton(
            f"Delete {len(selected)} file(s)", QDialogButtonBox.ButtonRole.DestructiveRole
        )
        confirm.setObjectName("danger")
        back.clicked.connect(review.reject)
        confirm.clicked.connect(
            lambda: self._delete_files(selected, review, confirm, back)
        )
        layout.addWidget(actions)
        review.exec()

    def _delete_files(self, files, review, confirm, back):
        branch = self.branch.currentText()
        confirm.setEnabled(False)
        back.setEnabled(False)
        self.status.setText("Deleting reviewed files…")
        self.activity.emit(
            f"Starting deletion from {self.repository.full_name} ({branch}): {len(files)} file(s).",
            False,
        )
        deleted: list[str] = []
        failures: list[str] = []

        def delete_all(progress=None):
            for index, file in enumerate(files, 1):
                try:
                    commit = self.api.delete_file(
                        self.token, self.repository, branch, file
                    )
                    deleted.append(file.path)
                    if progress:
                        progress(f"Deleted {index}/{len(files)}: {file.path} ({commit[:7]})")
                except Exception as error:
                    failures.append(f"{file.path}: {error}")
                    if progress:
                        progress(f"Delete failed for {file.path}: {error}")
            return deleted, failures

        def completed(result):
            removed, errors = result
            review.accept()
            if removed:
                self._load_files(branch)
            if errors:
                self.activity.emit(
                    f"Deletion partially completed: {len(removed)} of {len(files)} file(s).",
                    True,
                )
                message = (
                    f"Deleted {len(removed)} of {len(files)} file(s). "
                    f"Failed paths:\n\n" + "\n".join(errors)
                )
                QMessageBox.warning(self, "Deletion partially completed", message)
            else:
                self.activity.emit(
                    f"Deletion complete: {len(removed)} file(s) deleted from {self.repository.full_name} ({branch}).",
                    False,
                )
                QMessageBox.information(
                    self,
                    "Deletion complete",
                    f"Deleted {len(removed)} file(s) from {self.repository.full_name}.",
                )

        def failed(error):
            confirm.setEnabled(True)
            back.setEnabled(True)
            self.status.setText(str(error))
            self.activity.emit(f"Deletion failed: {error}", True)

        self._start(
            delete_all,
            completed,
            failed,
            progress=lambda message: (
                self.status.setText(str(message)),
                self.activity.emit(
                    str(message),
                    str(message).startswith("Delete failed"),
                ),
            ),
        )

    def _start(self, function, on_success, on_error=None, progress=None):
        self.refresh.setEnabled(False)
        self.branch.setEnabled(False)
        self.select_all.setEnabled(False)
        self.clear.setEnabled(False)
        self.delete_button.setEnabled(False)

        def done(result):
            self._enable_controls()
            on_success(result)

        def failed(error):
            self._enable_controls()
            self.status.setText(str(error))
            if on_error:
                on_error(error)

        self.worker = create_worker(
            function,
            done,
            failed,
            progress
            or (lambda message: self.status.setText(str(message))),
        )

    def _enable_controls(self):
        self.refresh.setEnabled(True)
        self.branch.setEnabled(bool(self.branches))
        self.select_all.setEnabled(bool(self.files))
        self.clear.setEnabled(bool(self.files))
        self.delete_button.setEnabled(bool(self._selected_files()))


class GithubManagerWindow(QMainWindow):
    def __init__(self, restore_credentials: bool = True):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            initial_w = max(720, min(1200, int(avail.width() * 0.95)))
            initial_h = max(480, min(800, int(avail.height() * 0.92)))
            self.resize(initial_w, initial_h)
        else:
            self.resize(960, 600)
        self.setMinimumSize(720, 480)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        icon = QIcon(str(resource_path("GithubManagerIcon.png")))
        if not icon.isNull():
            self.setWindowIcon(icon)
        app = QApplication.instance()
        if app and not app.styleSheet():
            app.setStyleSheet(build_stylesheet())
        self.settings = QSettings(ORGANIZATION, APP_NAME)
        self.api = GitHubAPI()
        self.token: str | None = None
        self.account_name: str | None = None
        self.repositories: list[Repository] = []
        self._selected_repository_name: str | None = None
        self._repositories_loading = False
        self._repositories_error: str | None = None
        self.folder: Path | None = None
        self.local_files: dict[str, Path] = {}
        self._busy = False
        self._workers: list[Worker] = []
        self._auth_cancel: threading.Event | None = None
        self._connection_dialog: ConnectionDialog | None = None
        self._build_ui()
        if restore_credentials:
            self._restore_credentials()

    def _build_ui(self):
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(22, 17, 22, 18)
        outer.setSpacing(12)
        header = QHBoxLayout()
        title_group = QVBoxLayout()
        title = QLabel("Github Manager")
        title.setObjectName("title")
        title_group.addWidget(title)
        subtitle = QLabel("Choose files, review, and sync them with GitHub.")
        subtitle.setObjectName("subtitle")
        title_group.addWidget(subtitle)
        header.addLayout(title_group)
        header.addStretch(1)
        self.account_label = QLabel("Not connected")
        self.account_label.setObjectName("hint")
        header.addWidget(self.account_label)
        self.connect_button = QPushButton("Connect account")
        self.connect_button.clicked.connect(self._open_connection)
        header.addWidget(self.connect_button)
        self.sign_out_button = QPushButton("Sign out")
        self.sign_out_button.clicked.connect(self._sign_out)
        self.sign_out_button.hide()
        header.addWidget(self.sign_out_button)
        outer.addLayout(header)

        content = QSplitter(Qt.Orientation.Horizontal)
        content.setChildrenCollapsible(False)
        content.addWidget(self._build_github_panel())
        content.addWidget(self._build_local_panel())
        content.setSizes([540, 650])
        content.setStretchFactor(0, 1)
        content.setStretchFactor(1, 1)
        outer.addWidget(content, 1)

        status_panel, status_layout = make_panel("Activity")
        self.status_label = QLabel("Choose a local folder to get started.")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        status_layout.addWidget(self.progress)
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumBlockCount(200)
        self.activity.setPlaceholderText("Uploads, downloads, repository actions, and errors appear here.")
        self.activity.setMaximumHeight(125)
        status_layout.addWidget(self.activity)
        outer.addWidget(status_panel)
        foot = QLabel(
            "GitHub authorization uses the repo scope. Review access before approving; "
            "downloads can replace files with matching paths."
        )
        foot.setObjectName("hint")
        foot.setWordWrap(True)
        outer.addWidget(foot)
        self.setCentralWidget(central)

    def _build_local_panel(self) -> QWidget:
        panel, layout = make_panel("Local folder")
        folder_row = QHBoxLayout()
        self.folder_label = QLabel("No folder selected")
        self.folder_label.setObjectName("hint")
        self.folder_label.setWordWrap(True)
        self.browse_button = QPushButton("Browse…")
        self.browse_button.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.browse_button)
        layout.addLayout(folder_row)
        controls = QHBoxLayout()
        self.selection_count = QLabel("0 files")
        self.select_all_button = QPushButton("Select all")
        self.clear_button = QPushButton("Clear")
        self.select_all_button.clicked.connect(lambda: self._set_files_selected(True))
        self.clear_button.clicked.connect(lambda: self._set_files_selected(False))
        controls.addWidget(QLabel("Files"))
        controls.addStretch(1)
        controls.addWidget(self.selection_count)
        controls.addWidget(self.select_all_button)
        controls.addWidget(self.clear_button)
        layout.addLayout(controls)
        self.local_list = QListWidget()
        self.local_list.itemChanged.connect(self._update_selected_count)
        layout.addWidget(self.local_list, 1)
        self.commit_message = QLineEdit("Sync selected files")
        self.commit_message.setPlaceholderText("Commit message")
        layout.addWidget(self.commit_message)
        self.upload_button = QPushButton("Upload selected files to GitHub")
        self.upload_button.clicked.connect(self._upload)
        layout.addWidget(self.upload_button)
        return panel

    def _build_github_panel(self) -> QWidget:
        panel, layout = make_panel("GitHub repositories")
        repository_row = QHBoxLayout()
        self.search_repositories = QLineEdit()
        self.search_repositories.setPlaceholderText("Search repositories")
        self.search_repositories.textChanged.connect(self._populate_repositories)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self._refresh_repositories)
        repository_row.addWidget(self.search_repositories, 1)
        repository_row.addWidget(self.refresh_button)
        layout.addLayout(repository_row)
        self.repository_combo = QComboBox()
        self.repository_combo.setObjectName("repositoryDropdown")
        self.repository_combo.setEditable(False)
        self.repository_combo.setMaxVisibleItems(20)
        self.repository_combo.setPlaceholderText("Connect GitHub to load repositories")
        self.repository_combo.setMinimumHeight(38)
        self.repository_combo.setToolTip(
            "Choose the repository for upload, download, and repository actions."
        )
        self.repository_combo.currentIndexChanged.connect(
            self._repository_selection_changed
        )
        layout.addWidget(self.repository_combo)
        self.visibility_label = QLabel("Select a repository")
        self.visibility_label.setObjectName("hint")
        layout.addWidget(self.visibility_label)
        self.visibility_button = QPushButton("Make public…")
        self.visibility_button.clicked.connect(self._change_visibility)
        layout.addWidget(self.visibility_button)
        self.create_repository_button = QPushButton("Create repository")
        self.create_repository_button.clicked.connect(self._create_repository)
        layout.addWidget(self.create_repository_button)
        self.download_button = QPushButton("Download repository")
        self.download_button.clicked.connect(self._download)
        layout.addWidget(self.download_button)
        self.delete_button = QPushButton("Delete repository files…")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete_files)
        layout.addWidget(self.delete_button)
        layout.addStretch(1)
        scopes = QLabel(
            "Private by default · Uploads create a commit · "
            "Repository deletion requires a separate path-by-path review"
        )
        scopes.setObjectName("hint")
        scopes.setWordWrap(True)
        layout.addWidget(scopes)
        return panel

    def _log(self, message: str, error: bool = False):
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = "ERROR  " if error else ""
        self.activity.appendPlainText(f"{timestamp}  {prefix}{message}")

    def _set_status(self, message: str, busy: bool | None = None):
        self.status_label.setText(message)
        if busy is not None:
            self._busy = busy
            self.progress.setVisible(busy)
            self._set_controls_enabled(not busy)

    def _set_controls_enabled(self, enabled: bool):
        for widget in (
            self.connect_button,
            self.sign_out_button,
            self.browse_button,
            self.select_all_button,
            self.clear_button,
            self.upload_button,
            self.refresh_button,
            self.search_repositories,
            self.repository_combo,
            self.visibility_button,
            self.create_repository_button,
            self.download_button,
            self.delete_button,
            self.commit_message,
            self.local_list,
        ):
            widget.setEnabled(enabled)
        self._update_selected_count()
        self._repository_changed()

    def _run_task(
        self,
        message: str,
        function: Callable,
        on_success: Callable,
        *,
        on_error: Callable | None = None,
        progress: Callable | None = None,
    ) -> bool:
        if self._busy:
            return False
        self._set_status(message, True)

        def completed(result):
            self._set_status(self.status_label.text(), False)
            try:
                on_success(result)
            except Exception as error:
                self._task_failed(error)

        def failed(error):
            self._set_status(self.status_label.text(), False)
            try:
                if on_error:
                    on_error(error)
                else:
                    self._task_failed(error)
            except Exception as callback_error:
                self._task_failed(callback_error)

        worker = Worker(function)
        worker.signals.result.connect(completed)
        worker.signals.error.connect(failed)
        if progress:
            worker.signals.progress.connect(progress)
        self._workers.append(worker)
        worker.signals.result.connect(lambda _result, item=worker: self._discard_worker(item))
        worker.signals.error.connect(lambda _error, item=worker: self._discard_worker(item))
        QThreadPool.globalInstance().start(worker)
        return True

    def _discard_worker(self, worker: Worker):
        if worker in self._workers:
            self._workers.remove(worker)

    def _task_failed(self, error: Exception):
        message = str(error)
        self._set_status(message)
        self._log(message, error=True)

    def _restore_credentials(self):
        self._repositories_loading = True
        self._populate_repositories()

        def restore():
            token = CredentialStore.load()
            if not token:
                return None
            user = self.api.get_user(token)
            repositories = self.api.list_repositories(token)
            return token, user, repositories

        self._run_task(
            "Checking saved GitHub authorization…",
            restore,
            self._restored,
            on_error=self._restore_failed,
        )

    def _restore_failed(self, error):
        self._repositories_loading = False
        self._repositories_error = str(error)
        self._populate_repositories()
        self._set_status(f"Could not restore GitHub authorization: {error}")
        self._log(f"Could not restore GitHub authorization: {error}", error=True)

    def _restored(self, result):
        self._repositories_loading = False
        self._repositories_error = None
        if result is None:
            self._populate_repositories()
            self._set_status("Choose a local folder to get started.")
            return
        self.token, user, repositories = result
        self.account_name = user.get("login", "GitHub user")
        self.repositories = repositories
        if self._selected_repository_name not in {
            repository.full_name for repository in repositories
        }:
            self._selected_repository_name = None
        self._update_account()
        self._populate_repositories()
        self._log(f"Connected to GitHub as @{self.account_name}.")
        self._set_status(f"Connected to GitHub as @{self.account_name}.")

    def _update_account(self):
        if self.account_name:
            self.account_label.setText(f"Connected as @{self.account_name}")
            self.connect_button.setText("Change account")
            self.sign_out_button.show()
        else:
            self.account_label.setText("Not connected")
            self.connect_button.setText("Connect account")
            self.sign_out_button.hide()
        self.create_repository_button.setEnabled(not self._busy and bool(self.token))
        self.refresh_button.setEnabled(not self._busy and bool(self.token))

    def _open_connection(self):
        dialog = ConnectionDialog(
            self.settings.value("githubOAuthClientID", "", type=str), self
        )
        self._connection_dialog = dialog
        dialog.authorize_requested.connect(self._authorize)
        dialog.cancel_requested.connect(self._cancel_authorization)
        dialog.finished.connect(lambda _result: self._clear_connection_dialog(dialog))
        dialog.exec()

    def _clear_connection_dialog(self, dialog):
        if self._connection_dialog is dialog:
            self._connection_dialog = None
        self._cancel_authorization()

    def _authorize(self, client_id: str):
        if not client_id:
            self._connection_dialog.show_error("Enter your public OAuth App Client ID.")
            return
        if self._busy:
            return
        self.settings.setValue("githubOAuthClientID", client_id)
        self._auth_cancel = threading.Event()
        cancel = self._auth_cancel
        dialog = self._connection_dialog
        if dialog:
            dialog.set_waiting(True)
            dialog.show_progress("Requesting a GitHub device code…")

        def authorize(progress=None):
            return authenticate_github(
                self.api,
                client_id,
                CredentialStore.save,
                progress=progress,
                cancelled=cancel.is_set,
            )

        def authorization_progress(value):
            current = self._connection_dialog
            if current is None:
                return
            kind, payload = value
            if kind == "device":
                current.show_device_code(payload)
                try:
                    from PySide6.QtCore import QUrl
                    from PySide6.QtGui import QDesktopServices

                    QDesktopServices.openUrl(QUrl(payload.verification_uri))
                except Exception:
                    pass
            elif kind == "status":
                current.show_progress(payload)

        def connected(result):
            self.token, user, self.repositories, repository_error = result
            self.account_name = user.get("login", "GitHub user")
            self._repositories_loading = False
            self._repositories_error = (
                str(repository_error) if repository_error is not None else None
            )
            self.search_repositories.clear()
            self._update_account()
            self._populate_repositories()
            self._log(f"Connected to GitHub as @{self.account_name}.")
            if repository_error is not None:
                self._log(
                    f"Could not load repositories: {repository_error}",
                    error=True,
                )
                self._set_status(
                    f"Connected as @{self.account_name}, but repository loading failed. "
                    "Use Refresh to retry."
                )
            else:
                self._set_status(f"Connected to GitHub as @{self.account_name}.")
            if self._connection_dialog:
                self._connection_dialog.accept()

        def failed(error):
            if cancel.is_set():
                self._set_status("Authorization cancelled.")
                return
            self._log(f"GitHub authorization failed: {error}", error=True)
            self._set_status("Could not connect to GitHub.")
            if self._connection_dialog:
                self._connection_dialog.set_waiting(False)
                self._connection_dialog.show_error(str(error))

        self._run_task(
            "Authorizing GitHub…",
            authorize,
            connected,
            on_error=failed,
            progress=authorization_progress,
        )

    def _cancel_authorization(self):
        if self._auth_cancel:
            self._auth_cancel.set()
            self._auth_cancel = None

    def _sign_out(self):
        try:
            CredentialStore.delete()
        except CredentialStoreError as error:
            self._task_failed(error)
            return
        self.token = None
        self.account_name = None
        self.repositories.clear()
        self._selected_repository_name = None
        self._repositories_loading = False
        self._repositories_error = None
        self.search_repositories.clear()
        self._populate_repositories()
        self._update_account()
        self._log("Signed out. The GitHub authorization was removed from secure storage.")
        self._set_status("Signed out of GitHub.")

    def _choose_folder(self):
        selected = QFileDialog.getExistingDirectory(self, "Choose a local folder")
        if not selected:
            return
        root = Path(selected)

        def scanned(files):
            self.folder = root
            self.local_files = dict(files)
            self.folder_label.setText(str(root))
            self.folder_label.setToolTip(str(root))
            self.local_list.blockSignals(True)
            self.local_list.clear()
            for relative, _path in files:
                item = QListWidgetItem(relative)
                item.setData(Qt.ItemDataRole.UserRole, relative)
                item.setToolTip(relative)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.local_list.addItem(item)
            self.local_list.blockSignals(False)
            self._update_selected_count()
            self._log(f"Scanned {len(files)} file(s) in {root.name}.")
            self._set_status(f"Found {len(files)} file(s) in {root.name}.")

        self._run_task(
            "Scanning local folder…",
            lambda: scan_local_files(root),
            scanned,
            on_error=self._task_failed,
        )

    def _selected_local_files(self) -> list[tuple[str, Path]]:
        return [
            (relative, self.local_files[relative])
            for index in range(self.local_list.count())
            if self.local_list.item(index).checkState() == Qt.CheckState.Checked
            for relative in [self.local_list.item(index).data(Qt.ItemDataRole.UserRole)]
            if relative in self.local_files
        ]

    def _set_files_selected(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for index in range(self.local_list.count()):
            self.local_list.item(index).setCheckState(state)

    def _update_selected_count(self, *_):
        selected = len(self._selected_local_files())
        self.selection_count.setText(
            f"{selected} / {self.local_list.count()} selected"
            if self.local_list.count()
            else "0 files"
        )
        self.upload_button.setEnabled(
            not self._busy and self.local_list.count() > 0 and bool(self.token)
        )
        self.select_all_button.setEnabled(not self._busy and self.local_list.count() > 0)
        self.clear_button.setEnabled(not self._busy and selected > 0)

    def _repository(self) -> Repository | None:
        full_name = self.repository_combo.currentData()
        return next((repo for repo in self.repositories if repo.full_name == full_name), None)

    def _populate_repositories(self, *_):
        query = self.search_repositories.text().strip().casefold()
        filtered = [
            repo for repo in self.repositories if query in repo.full_name.casefold()
        ]
        current = self._selected_repository_name
        self.repository_combo.blockSignals(True)
        model = self.repository_combo.model()
        if not isinstance(model, QStandardItemModel):
            model = QStandardItemModel(self.repository_combo)
            self.repository_combo.setModel(model)
            model.itemChanged.connect(self._on_repository_item_checked)
        else:
            model.blockSignals(True)
            model.clear()
            model.blockSignals(False)

        for repo in filtered:
            visibility = "Private" if repo.private else "Public"
            item = QStandardItem(f"{repo.full_name} · {visibility} · {repo.default_branch}")
            item.setData(repo.full_name, Qt.ItemDataRole.UserRole)
            item.setCheckable(True)
            item.setCheckState(Qt.CheckState.Checked if repo.full_name == current else Qt.CheckState.Unchecked)
            model.appendRow(item)

        self.repository_combo.setPlaceholderText(self._repository_placeholder(query))
        index = self.repository_combo.findData(current)
        if index >= 0:
            self.repository_combo.setCurrentIndex(index)
        else:
            self.repository_combo.setCurrentIndex(-1)
        self.repository_combo.blockSignals(False)
        self._sync_repository_checkboxes()
        self._repository_changed()

    def _on_repository_item_checked(self, item: QStandardItem):
        if item.checkState() == Qt.CheckState.Checked:
            row = item.row()
            if self.repository_combo.currentIndex() != row:
                self.repository_combo.setCurrentIndex(row)
        elif self.repository_combo.currentIndex() == item.row():
            item.setCheckState(Qt.CheckState.Checked)

    def _sync_repository_checkboxes(self):
        model = self.repository_combo.model()
        if isinstance(model, QStandardItemModel):
            curr_idx = self.repository_combo.currentIndex()
            model.blockSignals(True)
            for row in range(model.rowCount()):
                it = model.item(row)
                if it is not None and it.isCheckable():
                    it.setCheckState(Qt.CheckState.Checked if row == curr_idx else Qt.CheckState.Unchecked)
            model.blockSignals(False)

    def _repository_placeholder(self, query: str = "") -> str:
        if not self.token:
            if self._repositories_error:
                return "Could not load repositories — reconnect to GitHub"
            if self._repositories_loading:
                return "Loading repositories…"
            return "Connect GitHub to load repositories"
        if self._repositories_loading and not self.repositories:
            return "Loading repositories…"
        if self._repositories_error and not self.repositories:
            return "Repository load failed — refresh to retry"
        if not self.repositories:
            return "No repositories available"
        if query and not any(
            query in repo.full_name.casefold() for repo in self.repositories
        ):
            return "No matches — change your search"
        return "Select a repository"

    def _repository_selection_changed(self, *_):
        selected = self.repository_combo.currentData()
        if selected is not None:
            self._selected_repository_name = selected
        self._sync_repository_checkboxes()
        self._repository_changed()

    def _repository_changed(self, *_):
        repo = self._repository()
        if repo:
            self.visibility_label.setText(
                f"{'Private' if repo.private else 'Public'} repository · Default branch: {repo.default_branch}"
            )
            self.visibility_button.setText(
                "Make public…" if repo.private else "Make private…"
            )
            self.visibility_button.setEnabled(not self._busy and bool(self.token))
            self.download_button.setEnabled(not self._busy and bool(self.token))
            self.delete_button.setEnabled(not self._busy and bool(self.token))
        else:
            query = self.search_repositories.text().strip().casefold()
            self.visibility_label.setText(self._repository_placeholder(query))
            self.visibility_button.setEnabled(False)
            self.download_button.setEnabled(False)
            self.delete_button.setEnabled(False)
        self.create_repository_button.setEnabled(not self._busy and bool(self.token))

    def _refresh_repositories(self):
        if not self.token:
            self._open_connection()
            return
        token = self.token
        self._repositories_loading = True
        self._repositories_error = None
        self._populate_repositories()

        def refreshed(repositories):
            self._repositories_loading = False
            self._repositories_error = None
            self.repositories = repositories
            if self._selected_repository_name not in {
                repository.full_name for repository in repositories
            }:
                self._selected_repository_name = None
            self._populate_repositories()
            self._log(f"Loaded {len(repositories)} accessible repository/repositories.")
            self._set_status(f"Loaded {len(repositories)} repositories.")

        def refresh_failed(error):
            self._repositories_loading = False
            self._repositories_error = str(error)
            self._populate_repositories()
            self._task_failed(error)

        self._run_task(
            "Refreshing repositories…",
            lambda: self.api.list_repositories(token),
            refreshed,
            on_error=refresh_failed,
        )

    def _create_repository(self):
        if not self.token:
            self._open_connection()
            return
        dialog = CreateRepositoryDialog(self._submit_repository, self)
        dialog.exec()

    def _submit_repository(self, name, description, private, dialog):
        token = self.token
        if not token:
            dialog.create_failed("Connect a GitHub account before creating a repository.")
            return

        def created(repository):
            self.repositories = [
                repo for repo in self.repositories if repo.full_name != repository.full_name
            ] + [repository]
            self.repositories.sort(key=lambda repo: repo.full_name.casefold())
            self.search_repositories.clear()
            self._selected_repository_name = repository.full_name
            self._populate_repositories()
            self.repository_combo.setCurrentIndex(
                self.repository_combo.findData(repository.full_name)
            )
            self._log(f"Created {'private' if private else 'public'} repository {repository.full_name}.")
            self._set_status(f"Created repository {repository.full_name}.")
            dialog.create_succeeded()

        self._run_task(
            f"Creating {'private' if private else 'public'} repository…",
            lambda: self.api.create_repository(token, name, description, private),
            created,
            on_error=lambda error: dialog.create_failed(str(error)),
        )

    def _change_visibility(self):
        repository = self._repository()
        if not repository or not self.token:
            return
        make_private = not repository.private
        if repository.private:
            warning = (
                f"{repository.full_name} will become public. Anyone can view its content "
                "and commit history."
            )
        else:
            warning = (
                f"{repository.full_name} will become private. Public access is removed, "
                "but existing clones, forks, and copies cannot be recalled."
            )
        answer = QMessageBox.warning(
            self,
            "Change repository visibility?",
            warning,
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        token = self.token

        def changed(updated):
            self.repositories = [
                updated if item.full_name == updated.full_name else item
                for item in self.repositories
            ]
            self._populate_repositories()
            self.repository_combo.setCurrentIndex(
                self.repository_combo.findData(updated.full_name)
            )
            self._log(f"Visibility updated: {updated.full_name} is now {'private' if updated.private else 'public'}.")
            self._set_status(f"Changed {updated.full_name} visibility.")

        self._run_task(
            f"Changing {repository.full_name} visibility…",
            lambda: self.api.update_visibility(token, repository, make_private),
            changed,
            on_error=self._task_failed,
        )

    def _upload(self):
        if not self.token:
            self._open_connection()
            return
        repository = self._repository()
        files = self._selected_local_files()
        message = self.commit_message.text().strip()
        if not self.folder:
            self._task_failed(GitHubError("Choose a local folder before uploading."))
            return
        if not repository:
            self._task_failed(GitHubError("Choose a GitHub repository before uploading."))
            return
        if not files:
            self._task_failed(GitHubError("Select at least one file to upload."))
            return
        if not message:
            self._task_failed(GitHubError("Enter a commit message."))
            return
        token = self.token
        self._log(f"Starting upload to {repository.full_name}: {len(files)} file(s).")

        def uploaded(sha):
            self._log(
                f"Upload complete: {len(files)} file(s) committed to {repository.full_name} ({sha[:7]})."
            )
            self._set_status(
                f"Committed {len(files)} file(s) to {repository.full_name} ({sha[:7]})."
            )

        self._run_task(
            f"Uploading {len(files)} file(s)…",
            lambda progress=None: self.api.upload_files(
                token, repository, files, message, progress
            ),
            uploaded,
            on_error=self._task_failed,
            progress=lambda text: self._log(str(text)),
        )

    def _download(self):
        if not self.token:
            self._open_connection()
            return
        repository = self._repository()
        if not repository:
            self._task_failed(GitHubError("Choose a GitHub repository before downloading."))
            return
        if not self.folder:
            selected = QFileDialog.getExistingDirectory(
                self, "Choose a destination folder for the repository"
            )
            if not selected:
                return
            self.folder = Path(selected)
            self.folder_label.setText(selected)
        token = self.token
        destination = self.folder
        self._log(f"Starting download from {repository.full_name} to {destination}.")

        def downloaded(count):
            self._log(f"Download complete: {count} file(s) from {repository.full_name}.")
            self._set_status(
                f"Downloaded {count} file(s) from {repository.full_name} to {destination.name}."
            )
            self._choose_folder_after_download(destination)

        self._run_task(
            f"Downloading {repository.full_name}…",
            lambda progress=None: self.api.download_repository(
                token, repository, destination, progress
            ),
            downloaded,
            on_error=self._task_failed,
            progress=lambda text: (
                self._log(str(text)),
                self._set_status(str(text)),
            ),
        )

    def _choose_folder_after_download(self, folder: Path):
        def scanned(files):
            self.folder = folder
            self.local_files = dict(files)
            self.folder_label.setText(str(folder))
            self.folder_label.setToolTip(str(folder))
            self.local_list.blockSignals(True)
            self.local_list.clear()
            for relative, _path in files:
                item = QListWidgetItem(relative)
                item.setData(Qt.ItemDataRole.UserRole, relative)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.local_list.addItem(item)
            self.local_list.blockSignals(False)
            self._update_selected_count()
            self._set_status(
                f"Downloaded folder is ready: {len(files)} file(s) in {folder.name}."
            )

        self._run_task(
            "Refreshing local file list…",
            lambda: scan_local_files(folder),
            scanned,
            on_error=self._task_failed,
        )

    def _delete_files(self):
        repository = self._repository()
        if not repository or not self.token:
            return
        dialog = DeleteFilesDialog(self.api, self.token, repository, self)
        dialog.activity.connect(self._log)
        dialog.exec()

    def closeEvent(self, event):
        self._cancel_authorization()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORGANIZATION)
    app.setStyleSheet(build_stylesheet())
    window = GithubManagerWindow()
    window.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
