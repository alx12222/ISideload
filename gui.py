"""
PyQt6 front end for iSideload. Drag-and-drop a .ipa, .p12, and .mobileprovision,
type the p12 password, pick a connected iPhone, hit Sign & Install.
"""

import os
import sys
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QFileSystemWatcher, QTimer
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QPlainTextEdit,
    QFileDialog, QHBoxLayout, QVBoxLayout, QGridLayout, QComboBox,
    QGroupBox, QMessageBox, QCheckBox,
)

from signer import SignJob, sign_ipa, inspect_provision, SignError
from device import (
    list_devices, install_ipa, Device, DeviceError,
    push_provisioning_profile, extract_bundle_id, wait_for_trust_and_launch,
)
from vault_browser import VaultBrowser, DEFAULT_VAULT_URL
from dns_dialog import DnsProfileDialog


# ---------- drop-aware line edit ----------

class DropLineEdit(QLineEdit):
    def __init__(self, accept_exts, placeholder, parent=None):
        super().__init__(parent)
        self.accept_exts = tuple(e.lower() for e in accept_exts)
        self.setPlaceholderText(placeholder)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e: QDragEnterEvent):
        md = e.mimeData()
        if md.hasUrls():
            for url in md.urls():
                if url.toLocalFile().lower().endswith(self.accept_exts):
                    e.acceptProposedAction()
                    return
        e.ignore()

    def dropEvent(self, e: QDropEvent):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith(self.accept_exts):
                self.setText(p)
                e.acceptProposedAction()
                return


# ---------- worker threads ----------

class SignThread(QThread):
    log_line = pyqtSignal(str)
    done = pyqtSignal(str)           # output path
    failed = pyqtSignal(str)         # error message

    def __init__(self, job: SignJob):
        super().__init__()
        self.job = job

    def run(self):
        try:
            out = sign_ipa(self.job, log=lambda m: self.log_line.emit(m))
            self.done.emit(out)
        except Exception as e:
            self.log_line.emit(traceback.format_exc())
            self.failed.emit(str(e))


class InstallThread(QThread):
    """
    Full-automation install:
      1. push provisioning profile via misagent (dev certs → no trust tap)
      2. install IPA via ideviceinstaller
      3. if signed with an enterprise cert and trust hasn't been granted
         for the team yet, poll trust state and auto-launch when it flips
    """
    log_line = pyqtSignal(str)
    done = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, udid: str, ipa_path: str,
                 mobileprovision_path: Optional[str] = None,
                 auto_trust_wait: bool = True):
        super().__init__()
        self.udid = udid
        self.ipa_path = ipa_path
        self.mobileprovision_path = mobileprovision_path
        self.auto_trust_wait = auto_trust_wait

    def run(self):
        emit = lambda m: self.log_line.emit(m)
        try:
            # Step 1: push the provisioning profile ahead of the IPA.
            if self.mobileprovision_path:
                try:
                    push_provisioning_profile(self.udid,
                                              self.mobileprovision_path,
                                              log=emit)
                except Exception as e:
                    # Non-fatal — profile may already be on device, or
                    # enterprise cert doesn't need it. Keep going.
                    emit(f"  (skipping profile push: {e})")

            # Step 2: install IPA.
            install_ipa(self.udid, self.ipa_path, log=emit)

            # Step 3: poll trust and auto-launch.
            if self.auto_trust_wait:
                bundle_id = extract_bundle_id(self.ipa_path)
                if bundle_id:
                    emit(f"Watching trust state for {bundle_id}…")
                    try:
                        wait_for_trust_and_launch(self.udid, bundle_id,
                                                  log=emit, timeout_sec=300)
                    except Exception as e:
                        emit(f"  (trust watch: {e})")
                else:
                    emit("  (couldn't read bundle id — skipping auto-launch)")

            self.done.emit()
        except Exception as e:
            self.log_line.emit(traceback.format_exc())
            self.failed.emit(str(e))


# ---------- main window ----------

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("iSideload")
        self.resize(820, 640)

        self.sign_thread = None
        self.install_thread = None
        self.last_signed_ipa = None
        self.vault_window = None
        self.vault_dir = Path(__file__).parent / "certs"
        self.vault_dir.mkdir(parents=True, exist_ok=True)

        # Snapshot of files already in the vault — anything new that appears
        # after launch is auto-imported.
        self._vault_seen = {p.name for p in self.vault_dir.iterdir() if p.is_file()}
        self._vault_watcher = QFileSystemWatcher([str(self.vault_dir)])
        self._vault_watcher.directoryChanged.connect(self._on_vault_dir_changed)
        self._vault_debounce = QTimer(self); self._vault_debounce.setSingleShot(True)
        self._vault_debounce.setInterval(400)
        self._vault_debounce.timeout.connect(self._scan_vault)

        root = QVBoxLayout(self)

        # --- Inputs group ---
        inputs = QGroupBox("Inputs  (drag files onto the fields)")
        grid = QGridLayout(inputs)

        self.ipa_edit = DropLineEdit([".ipa"], "Drop a .ipa here, or browse…")
        self.p12_edit = DropLineEdit([".p12", ".pfx"], "Drop a .p12 / .pfx certificate here…")
        self.mp_edit = DropLineEdit([".mobileprovision"], "Drop a .mobileprovision here…")

        self.pw_edit = QLineEdit()
        self.pw_edit.setPlaceholderText("P12 password")
        self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)

        self.show_pw = QCheckBox("show")
        self.show_pw.toggled.connect(
            lambda on: self.pw_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )

        grid.addWidget(QLabel("IPA:"), 0, 0)
        grid.addWidget(self.ipa_edit, 0, 1)
        grid.addWidget(self._browse_btn(self.ipa_edit, "IPA (*.ipa)"), 0, 2)

        grid.addWidget(QLabel("Cert (.p12):"), 1, 0)
        grid.addWidget(self.p12_edit, 1, 1)
        grid.addWidget(self._browse_btn(self.p12_edit, "Certificate (*.p12 *.pfx)"), 1, 2)

        grid.addWidget(QLabel("Password:"), 2, 0)
        pw_row = QHBoxLayout()
        pw_row.addWidget(self.pw_edit)
        pw_row.addWidget(self.show_pw)
        pw_wrap = QWidget(); pw_wrap.setLayout(pw_row)
        grid.addWidget(pw_wrap, 2, 1, 1, 2)

        grid.addWidget(QLabel("Provision:"), 3, 0)
        grid.addWidget(self.mp_edit, 3, 1)
        grid.addWidget(self._browse_btn(self.mp_edit, "Provisioning (*.mobileprovision)"), 3, 2)

        root.addWidget(inputs)

        # --- Optional overrides ---
        overrides = QGroupBox("Optional overrides (leave blank to keep IPA values)")
        og = QGridLayout(overrides)
        self.bid_edit = QLineEdit(); self.bid_edit.setPlaceholderText("com.example.app")
        self.bname_edit = QLineEdit(); self.bname_edit.setPlaceholderText("Display name")
        self.bver_edit = QLineEdit(); self.bver_edit.setPlaceholderText("1.0.0")
        og.addWidget(QLabel("Bundle ID:"), 0, 0); og.addWidget(self.bid_edit, 0, 1)
        og.addWidget(QLabel("Name:"), 0, 2); og.addWidget(self.bname_edit, 0, 3)
        og.addWidget(QLabel("Version:"), 0, 4); og.addWidget(self.bver_edit, 0, 5)
        root.addWidget(overrides)

        # --- Device + actions ---
        devbox = QGroupBox("Device")
        dv = QHBoxLayout(devbox)
        self.dev_combo = QComboBox()
        self.dev_combo.setMinimumWidth(380)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_devices)
        dv.addWidget(QLabel("Connected iPhone:"))
        dv.addWidget(self.dev_combo, 1)
        dv.addWidget(self.refresh_btn)
        root.addWidget(devbox)

        actions = QHBoxLayout()
        self.vault_btn = QPushButton("Browse Certificates")
        self.vault_btn.clicked.connect(self.open_vault_browser)
        self.dns_btn = QPushButton("Install DNS Profile…")
        self.dns_btn.clicked.connect(self.open_dns_dialog)
        self.inspect_btn = QPushButton("Inspect Provision")
        self.inspect_btn.clicked.connect(self.inspect_provision)
        self.sign_btn = QPushButton("Sign")
        self.sign_btn.clicked.connect(self.sign_only)
        self.sign_install_btn = QPushButton("Sign && Install")
        self.sign_install_btn.setDefault(True)
        f = QFont(); f.setBold(True); self.sign_install_btn.setFont(f)
        self.sign_install_btn.clicked.connect(self.sign_and_install)
        self.install_only_btn = QPushButton("Install Last Signed")
        self.install_only_btn.clicked.connect(self.install_last)
        actions.addWidget(self.vault_btn)
        actions.addWidget(self.dns_btn)
        actions.addWidget(self.inspect_btn)
        actions.addStretch(1)
        actions.addWidget(self.sign_btn)
        actions.addWidget(self.install_only_btn)
        actions.addWidget(self.sign_install_btn)
        root.addLayout(actions)

        # --- Log ---
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        root.addWidget(self.log, 1)

        self.refresh_devices()
        self._log("iSideload ready. Drop your IPA, cert, and provision, then Sign && Install.")

    # ---------- helpers ----------

    def _browse_btn(self, target: QLineEdit, filt: str) -> QPushButton:
        b = QPushButton("Browse…")
        def pick():
            p, _ = QFileDialog.getOpenFileName(self, "Choose file", "", filt)
            if p:
                target.setText(p)
        b.clicked.connect(pick)
        return b

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{ts}] {msg}")

    def _lock(self, locked: bool):
        for w in (self.sign_btn, self.sign_install_btn, self.install_only_btn, self.refresh_btn):
            w.setEnabled(not locked)

    def _build_job(self) -> SignJob:
        ipa = self.ipa_edit.text().strip()
        if not ipa:
            raise SignError("No IPA selected.")
        stem = Path(ipa).stem
        out_dir = Path(ipa).parent / "signed"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{stem}_signed.ipa"

        return SignJob(
            ipa_path=ipa,
            p12_path=self.p12_edit.text().strip(),
            p12_password=self.pw_edit.text(),
            mobileprovision_path=self.mp_edit.text().strip(),
            output_path=str(out),
            bundle_id=self.bid_edit.text().strip() or None,
            bundle_name=self.bname_edit.text().strip() or None,
            bundle_version=self.bver_edit.text().strip() or None,
        )

    def _current_device(self) -> Device | None:
        data = self.dev_combo.currentData()
        return data

    # ---------- actions ----------

    def refresh_devices(self):
        self.dev_combo.clear()
        try:
            devs = list_devices()
        except DeviceError as e:
            self._log(f"[!] {e}")
            return
        if not devs:
            self.dev_combo.addItem("— no iPhone detected —", None)
            self._log("No devices. Plug an iPhone in via USB and tap Trust on the phone.")
            return
        for d in devs:
            label = f"{d.name}  ·  iOS {d.ios_version}  ·  {d.product_type}  ·  {d.udid[:8]}…"
            self.dev_combo.addItem(label, d)
        self._log(f"Found {len(devs)} device(s).")

    def open_dns_dialog(self):
        dlg = DnsProfileDialog(self)
        dlg.exec()

    def open_vault_browser(self):
        if self.vault_window is not None and self.vault_window.isVisible():
            self.vault_window.raise_(); self.vault_window.activateWindow()
            return
        self.vault_window = VaultBrowser(self.vault_dir, start_url=DEFAULT_VAULT_URL)
        self.vault_window.file_saved.connect(self._on_vault_file_saved)
        self.vault_window.show()
        self._log(f"Vault browser open → {DEFAULT_VAULT_URL}")

    def _on_vault_dir_changed(self, _path: str):
        # Debounce: a browser may write the file in chunks (tempfile → rename).
        self._vault_debounce.start()

    def _scan_vault(self):
        """Pick up any new file in the vault folder and route it in."""
        current = {p.name for p in self.vault_dir.iterdir() if p.is_file()}
        new_names = current - self._vault_seen
        self._vault_seen = current
        # Unpack any .zip the vault receives into the vault itself.
        for name in list(new_names):
            full = self.vault_dir / name
            if full.suffix.lower() == ".zip":
                try:
                    import zipfile
                    with zipfile.ZipFile(full) as z:
                        z.extractall(self.vault_dir)
                    self._log(f"Unpacked {name} into vault.")
                except Exception as e:
                    self._log(f"[!] Unpack failed for {name}: {e}")
        # Re-scan after any unzip
        current = {p.name for p in self.vault_dir.rglob("*") if p.is_file()}
        new_names = current - self._vault_seen
        self._vault_seen = current

        # Route each interesting file by extension
        for name in sorted(new_names):
            # name may be a relative path if unzip created subdirs — resolve
            full = (self.vault_dir / name) if (self.vault_dir / name).exists() else None
            if full is None:
                for p in self.vault_dir.rglob(name):
                    full = p; break
            if full is None or not full.is_file():
                continue
            ext = full.suffix.lower()
            if ext in (".p12", ".pfx", ".mobileprovision", ".ipa"):
                self._on_vault_file_saved(str(full), ext)

    def _on_vault_file_saved(self, path: str, ext: str):
        """Route a freshly downloaded vault file into the right UI field."""
        self._log(f"Vault download: {path}")
        if ext in (".p12", ".pfx"):
            self.p12_edit.setText(path)
            self._autopair_from_p12(path)
            self.pw_edit.setFocus()  # user still needs to type the password
            self._maybe_prompt_password(path)
        elif ext == ".mobileprovision":
            self.mp_edit.setText(path)
            self._autopair_from_mp(path)
        elif ext == ".ipa":
            self.ipa_edit.setText(path)

    def _autopair_from_p12(self, p12_path: str):
        """If a matching .mobileprovision sits next to the .p12, grab it too."""
        stem = Path(p12_path).with_suffix("")
        for cand in (stem.with_suffix(".mobileprovision"),
                     Path(p12_path).parent / (Path(p12_path).stem + ".mobileprovision")):
            if cand.exists() and not self.mp_edit.text():
                self.mp_edit.setText(str(cand))
                self._log(f"Auto-paired provision: {cand}")
                return

    def _autopair_from_mp(self, mp_path: str):
        """If a matching .p12 sits next to the .mobileprovision, grab it too."""
        stem = Path(mp_path).with_suffix("")
        for ext in (".p12", ".pfx"):
            cand = stem.with_suffix(ext)
            if cand.exists() and not self.p12_edit.text():
                self.p12_edit.setText(str(cand))
                self._log(f"Auto-paired certificate: {cand}")
                self._maybe_prompt_password(str(cand))
                return

    def _maybe_prompt_password(self, p12_path: str):
        """Look for a password in a sibling .txt / .password file; else leave blank."""
        p = Path(p12_path)
        for cand in (p.with_suffix(".txt"),
                     p.with_suffix(".password"),
                     p.parent / "password.txt",
                     p.parent / f"{p.stem}.password.txt"):
            if cand.exists() and cand.stat().st_size < 4096:
                try:
                    pw = cand.read_text(encoding="utf-8", errors="ignore").strip()
                    # strip surrounding quotes if present
                    if len(pw) >= 2 and pw[0] in "\"'" and pw[-1] == pw[0]:
                        pw = pw[1:-1]
                    if pw:
                        self.pw_edit.setText(pw)
                        self._log(f"Auto-filled password from {cand.name}")
                        return
                except Exception:
                    pass

    def inspect_provision(self):
        mp = self.mp_edit.text().strip()
        if not mp:
            QMessageBox.warning(self, "iSideload", "Pick a .mobileprovision first.")
            return
        try:
            info = inspect_provision(mp)
        except Exception as e:
            self._log(f"[!] {e}")
            return
        self._log("Provision summary:")
        for k, v in info.items():
            self._log(f"    {k}: {v}")

    def sign_only(self):
        try:
            job = self._build_job()
        except SignError as e:
            QMessageBox.warning(self, "iSideload", str(e)); return

        self._lock(True)
        self._log("Signing…")
        self.sign_thread = SignThread(job)
        self.sign_thread.log_line.connect(self._log)
        self.sign_thread.done.connect(self._on_sign_done)
        self.sign_thread.failed.connect(self._on_sign_failed)
        self.sign_thread.start()

    def sign_and_install(self):
        dev = self._current_device()
        if dev is None:
            QMessageBox.warning(self, "iSideload", "No device selected. Plug one in and Refresh.")
            return
        try:
            job = self._build_job()
        except SignError as e:
            QMessageBox.warning(self, "iSideload", str(e)); return

        self._lock(True)
        self._log(f"Signing for {dev.name} ({dev.udid[:8]}…)")
        self.sign_thread = SignThread(job)
        self.sign_thread.log_line.connect(self._log)
        self.sign_thread.done.connect(lambda out, u=dev.udid: self._start_install(u, out))
        self.sign_thread.failed.connect(self._on_sign_failed)
        self.sign_thread.start()

    def install_last(self):
        if not self.last_signed_ipa or not Path(self.last_signed_ipa).exists():
            QMessageBox.information(self, "iSideload", "Sign something first.")
            return
        dev = self._current_device()
        if dev is None:
            QMessageBox.warning(self, "iSideload", "No device selected."); return
        self._start_install(dev.udid, self.last_signed_ipa)

    # ---------- callbacks ----------

    def _on_sign_done(self, out_path: str):
        self.last_signed_ipa = out_path
        self._log(f"Signed → {out_path}")
        self._lock(False)

    def _on_sign_failed(self, msg: str):
        self._log(f"[!] sign failed: {msg}")
        self._lock(False)

    def _start_install(self, udid: str, ipa_path: str):
        self.last_signed_ipa = ipa_path
        self._log(f"Installing {Path(ipa_path).name} to {udid[:8]}…")

        # Hand the provisioning profile to the installer so misagent can
        # push it ahead of the IPA → kills the Trust tap for dev certs.
        mp = self.mp_edit.text().strip() or None
        self.install_thread = InstallThread(
            udid, ipa_path,
            mobileprovision_path=mp,
            auto_trust_wait=True,
        )
        self.install_thread.log_line.connect(self._log)
        self.install_thread.done.connect(self._on_install_done)
        self.install_thread.failed.connect(self._on_install_failed)
        self.install_thread.start()

    def _on_install_done(self):
        self._log("[+] install complete.")
        self._lock(False)

    def _on_install_failed(self, msg: str):
        self._log(f"[!] install failed: {msg}")
        self._lock(False)


def run():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
