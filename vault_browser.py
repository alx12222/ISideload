"""
Embedded browser window for the certificate vault. Opens in-app with an
address bar and a "Download" watcher that auto-saves .p12 / .mobileprovision
/ .ipa files straight into the chosen vault folder.
"""

import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QUrl, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QLabel, QFileDialog, QMessageBox,
)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineDownloadRequest, QWebEngineProfile
    WEBENGINE_OK = True
except ImportError:
    WEBENGINE_OK = False


DEFAULT_VAULT_URL = "https://sideloading.net/certificates/"
INTERESTING_EXTS = (".p12", ".pfx", ".mobileprovision", ".ipa")


class VaultBrowser(QWidget):
    """
    Standalone window (not modal) that hosts a QWebEngineView.
    Downloads of cert / provision / ipa files are routed to a vault folder
    on disk and announced via a status line.
    """

    file_saved = pyqtSignal(str, str)  # (absolute_path, extension_lower_with_dot)

    def __init__(self, vault_dir: Path, start_url: str = DEFAULT_VAULT_URL, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Certificate Vault")
        self.resize(1100, 780)
        self.vault_dir = Path(vault_dir)
        self.vault_dir.mkdir(parents=True, exist_ok=True)

        root = QVBoxLayout(self)

        # Address bar
        bar = QHBoxLayout()
        self.back_btn = QPushButton("◀")
        self.fwd_btn = QPushButton("▶")
        self.reload_btn = QPushButton("⟳")
        self.addr = QLineEdit(start_url)
        self.go_btn = QPushButton("Go")
        self.vault_btn = QPushButton("Vault Folder…")
        for b in (self.back_btn, self.fwd_btn, self.reload_btn):
            b.setFixedWidth(34)
        bar.addWidget(self.back_btn)
        bar.addWidget(self.fwd_btn)
        bar.addWidget(self.reload_btn)
        bar.addWidget(self.addr, 1)
        bar.addWidget(self.go_btn)
        bar.addWidget(self.vault_btn)
        root.addLayout(bar)

        # Status line
        self.status = QLabel(f"Vault: {self.vault_dir}")
        self.status.setStyleSheet("color:#555; padding:2px 6px;")
        root.addWidget(self.status)

        # Browser
        if not WEBENGINE_OK:
            root.addWidget(QLabel(
                "PyQt6-WebEngine not installed.\n\n"
                "Run:  pip install PyQt6-WebEngine\n\n"
                "Then reopen this window."
            ))
            return

        self.view = QWebEngineView(self)
        root.addWidget(self.view, 1)

        # Hook download handler on the default profile so every nav triggers it
        profile = QWebEngineProfile.defaultProfile()
        profile.downloadRequested.connect(self._on_download)

        # Wire bar
        self.go_btn.clicked.connect(self._go)
        self.addr.returnPressed.connect(self._go)
        self.back_btn.clicked.connect(self.view.back)
        self.fwd_btn.clicked.connect(self.view.forward)
        self.reload_btn.clicked.connect(self.view.reload)
        self.vault_btn.clicked.connect(self._pick_vault)
        self.view.urlChanged.connect(lambda u: self.addr.setText(u.toString()))

        self.view.load(QUrl(start_url))

    def _go(self):
        text = self.addr.text().strip()
        if not text:
            return
        if "://" not in text:
            text = "https://" + text
        self.view.load(QUrl(text))

    def _pick_vault(self):
        d = QFileDialog.getExistingDirectory(self, "Choose vault folder", str(self.vault_dir))
        if d:
            self.vault_dir = Path(d)
            self.vault_dir.mkdir(parents=True, exist_ok=True)
            self.status.setText(f"Vault: {self.vault_dir}")

    def _on_download(self, dl: "QWebEngineDownloadRequest"):
        """
        Any download triggered inside the webview lands here. Files with
        extensions we care about go straight into the vault; anything else
        asks the user where to save.
        """
        name = dl.downloadFileName() or "download.bin"
        ext = os.path.splitext(name)[1].lower()

        if ext in INTERESTING_EXTS:
            target_dir = self.vault_dir
            # Avoid clobbering: if a file with the same name exists, suffix _1, _2…
            base, e = os.path.splitext(name)
            candidate = target_dir / name
            n = 1
            while candidate.exists():
                candidate = target_dir / f"{base}_{n}{e}"
                n += 1
            dl.setDownloadDirectory(str(target_dir))
            dl.setDownloadFileName(candidate.name)
            self.status.setText(f"Downloading → {candidate}")
        else:
            # Ask where to save for anything else
            target, _ = QFileDialog.getSaveFileName(
                self, "Save file", str(Path.home() / "Downloads" / name),
            )
            if not target:
                dl.cancel()
                return
            dl.setDownloadDirectory(str(Path(target).parent))
            dl.setDownloadFileName(Path(target).name)
            self.status.setText(f"Downloading → {target}")

        dl.isFinishedChanged.connect(lambda: self._on_finished(dl))
        dl.accept()

    def _on_finished(self, dl):
        if not dl.isFinished():
            return
        state = dl.state()
        name = dl.downloadFileName()
        path = Path(dl.downloadDirectory()) / name
        if state == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            self.status.setText(f"Saved: {path}")
            ext = path.suffix.lower()
            if ext in INTERESTING_EXTS:
                self.file_saved.emit(str(path), ext)
        else:
            self.status.setText(f"Download failed: {name}  ({state.name})")
