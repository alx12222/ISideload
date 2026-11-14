"""
DNS Profile dialog. Pick a preset or define a custom DoH/DoT, then press
Serve: the dialog spins up a local HTTP server, draws a QR code pointing
at the .mobileconfig, and tells the user to scan it with their iPhone.
"""

from io import BytesIO
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPixmap, QImage, QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QComboBox, QLineEdit,
    QLabel, QPushButton, QPlainTextEdit, QGroupBox, QRadioButton,
    QButtonGroup, QMessageBox, QFileDialog, QCheckBox,
)
from PyQt6.QtCore import QThread, pyqtSignal

import qrcode

from dns_profile import PRESETS, DNSPreset, build_mobileconfig
from profile_server import ProfileServer
from device import list_devices, install_profile_usb, DeviceError


class _UsbInstallThread(QThread):
    log_line = pyqtSignal(str)
    done = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, udid: str, payload: bytes):
        super().__init__()
        self.udid = udid
        self.payload = payload

    def run(self):
        try:
            install_profile_usb(self.udid, self.payload,
                                log=lambda m: self.log_line.emit(m))
            self.done.emit()
        except Exception as e:
            self.failed.emit(str(e))


def _pixmap_from_qr(data: str, box_size: int = 10) -> QPixmap:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    pm = QPixmap()
    pm.loadFromData(buf.getvalue(), "PNG")
    return pm


class DnsProfileDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Install DNS Profile")
        self.resize(720, 620)

        self.server: ProfileServer | None = None
        self._loaded_path: Path | None = None

        root = QVBoxLayout(self)

        # --- Preset / custom row ---
        pick_box = QGroupBox("Choose profile")
        pg = QGridLayout(pick_box)
        self.preset_combo = QComboBox()
        for p in PRESETS:
            self.preset_combo.addItem(p.name, p)
        self.preset_combo.addItem("— Custom —", None)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_change)

        pg.addWidget(QLabel("Preset:"), 0, 0)
        pg.addWidget(self.preset_combo, 0, 1, 1, 3)

        # Display name override
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Display name shown in iOS Settings")
        pg.addWidget(QLabel("Name:"), 1, 0)
        pg.addWidget(self.name_edit, 1, 1, 1, 3)

        # Protocol radio
        pg.addWidget(QLabel("Protocol:"), 2, 0)
        self.rb_https = QRadioButton("DoH (HTTPS)")
        self.rb_tls = QRadioButton("DoT (TLS)")
        self.rb_https.setChecked(True)
        self.proto_group = QButtonGroup(self)
        self.proto_group.addButton(self.rb_https); self.proto_group.addButton(self.rb_tls)
        self.rb_https.toggled.connect(self._refresh_proto_fields)
        pg.addWidget(self.rb_https, 2, 1); pg.addWidget(self.rb_tls, 2, 2)

        # DoH URL
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://dns.example.com/dns-query")
        pg.addWidget(QLabel("DoH URL:"), 3, 0)
        pg.addWidget(self.url_edit, 3, 1, 1, 3)

        # DoT hostname
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("dns.example.com")
        pg.addWidget(QLabel("DoT host:"), 4, 0)
        pg.addWidget(self.host_edit, 4, 1, 1, 3)

        # Fallback IPs
        self.ips_edit = QLineEdit()
        self.ips_edit.setPlaceholderText("1.1.1.1, 1.0.0.1, 2606:4700:4700::1111  (optional)")
        pg.addWidget(QLabel("Server IPs:"), 5, 0)
        pg.addWidget(self.ips_edit, 5, 1, 1, 3)

        # On-demand + identifier
        self.on_demand = QCheckBox("Always-on (recommended)")
        self.on_demand.setChecked(True)
        pg.addWidget(self.on_demand, 6, 1, 1, 2)

        self.ident_edit = QLineEdit()
        self.ident_edit.setPlaceholderText("Reverse-DNS identifier (auto if blank)")
        pg.addWidget(QLabel("Identifier:"), 7, 0)
        pg.addWidget(self.ident_edit, 7, 1, 1, 3)

        root.addWidget(pick_box)

        # --- Actions ---
        actions = QHBoxLayout()
        self.load_btn = QPushButton("Load .mobileconfig…")
        self.load_btn.setToolTip("Serve an existing .mobileconfig file instead of generating one")
        self.load_btn.clicked.connect(self._load_existing)
        self.save_btn = QPushButton("Save .mobileconfig…")
        self.save_btn.clicked.connect(self._save_profile)
        self.usb_btn = QPushButton("Install via USB")
        self.usb_btn.setToolTip("Push the profile straight to the connected iPhone — no Wi-Fi needed")
        f = QFont(); f.setBold(True); self.usb_btn.setFont(f)
        self.usb_btn.clicked.connect(self._install_usb)
        self.serve_btn = QPushButton("Serve && Show QR")
        self.serve_btn.clicked.connect(self._serve)
        self.stop_btn = QPushButton("Stop Server")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        actions.addWidget(self.load_btn); actions.addWidget(self.save_btn)
        actions.addStretch(1)
        actions.addWidget(self.stop_btn); actions.addWidget(self.serve_btn)
        actions.addWidget(self.usb_btn)
        root.addLayout(actions)

        self._usb_thread: _UsbInstallThread | None = None

        # --- QR display + info ---
        qr_row = QHBoxLayout()
        self.qr_label = QLabel()
        self.qr_label.setFixedSize(300, 300)
        self.qr_label.setStyleSheet("background:#eee; border:1px solid #ccc;")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setText("QR appears here\nafter Serve")
        qr_row.addWidget(self.qr_label)

        info = QVBoxLayout()
        self.url_label = QLabel("Server URL: (not running)")
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.timer_label = QLabel("Timer: —")
        self.steps = QPlainTextEdit()
        self.steps.setReadOnly(True)
        self.steps.setPlainText(
            "How to install\n\n"
            "FASTEST — Install via USB (no Wi-Fi):\n"
            "  1. Plug iPhone in, tap Trust if prompted.\n"
            "  2. Press Install via USB.\n"
            "  3. On the phone: Settings → General → VPN & Device Management\n"
            "     → Downloaded Profile → Install. Enter passcode, confirm.\n\n"
            "OR — QR + Wi-Fi (phone and PC on same network):\n"
            "  1. Press Serve && Show QR.\n"
            "  2. iPhone Camera → scan code → tap Safari banner.\n"
            "  3. Settings → General → VPN & Device Management → Install.\n\n"
            "iOS will show 'Unverified' for unsigned profiles — normal.\n"
            "Remove any time at the same Settings page."
        )
        info.addWidget(self.url_label)
        info.addWidget(self.timer_label)
        info.addWidget(self.steps, 1)
        qr_row.addLayout(info, 1)
        root.addLayout(qr_row)

        # Activity log
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(90)
        self.log.setFont(QFont("Consolas", 9))
        root.addWidget(self.log)

        # Timer tick
        self.tick = QTimer(self); self.tick.setInterval(500)
        self.tick.timeout.connect(self._tick)
        self.tick.start()

        self._on_preset_change(0)

    # ---------- helpers ----------

    def _log(self, m: str):
        self.log.appendPlainText(m)

    def _refresh_proto_fields(self):
        is_https = self.rb_https.isChecked()
        self.url_edit.setEnabled(is_https)
        self.host_edit.setEnabled(not is_https)

    def _on_preset_change(self, _idx: int):
        # Any user-driven preset change means we're back in generate-mode.
        self._loaded_path = None
        p: DNSPreset | None = self.preset_combo.currentData()
        if p is None:
            self.name_edit.setText("")
            self.url_edit.setText(""); self.host_edit.setText("")
            self.ips_edit.setText(""); self.ident_edit.setText("")
            self.rb_https.setChecked(True)
            self._refresh_proto_fields()
            return
        self.name_edit.setText(p.name)
        self.ident_edit.setText(p.identifier)
        if p.protocol == "HTTPS":
            self.rb_https.setChecked(True)
            self.url_edit.setText(p.server_url)
            self.host_edit.setText("")
        else:
            self.rb_tls.setChecked(True)
            self.host_edit.setText(p.server_name)
            self.url_edit.setText("")
        self.ips_edit.setText(", ".join(p.server_addresses))
        self._refresh_proto_fields()

    def _build_preset_from_form(self) -> DNSPreset:
        name = self.name_edit.text().strip() or "Custom DNS"
        ident = self.ident_edit.text().strip() or "net.iSideload.dns.custom"
        proto = "HTTPS" if self.rb_https.isChecked() else "TLS"
        ips = [s.strip() for s in self.ips_edit.text().split(",") if s.strip()]
        return DNSPreset(
            name=name,
            identifier=ident,
            protocol=proto,
            server_url=self.url_edit.text().strip() if proto == "HTTPS" else "",
            server_name=self.host_edit.text().strip() if proto == "TLS" else "",
            server_addresses=ips,
        )

    def _payload(self) -> bytes:
        if self._loaded_path is not None:
            return self._loaded_path.read_bytes()
        preset = self._build_preset_from_form()
        return build_mobileconfig(preset, on_demand=self.on_demand.isChecked())

    def _load_existing(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Pick a .mobileconfig",
            "", "Profiles (*.mobileconfig);;All files (*.*)"
        )
        if not path:
            return
        p = Path(path)
        data = p.read_bytes()
        info = self._inspect_profile(data)
        self._loaded_path = p
        # Reflect the loaded file in the form (read-only hints)
        self.name_edit.setText(info.get("display", p.stem))
        self.ident_edit.setText(info.get("identifier", ""))
        proto = info.get("protocol")
        if proto == "HTTPS":
            self.rb_https.setChecked(True)
            self.url_edit.setText(info.get("server_url", ""))
            self.host_edit.setText("")
        elif proto == "TLS":
            self.rb_tls.setChecked(True)
            self.host_edit.setText(info.get("server_name", ""))
            self.url_edit.setText("")
        self.ips_edit.setText(", ".join(info.get("server_addresses", [])))
        # Lock preset selector to indicate we're serving a loaded file
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(self.preset_combo.count() - 1)  # Custom
        self.preset_combo.blockSignals(False)
        self._refresh_proto_fields()
        self._log(f"Loaded profile: {p}  ({len(data)} bytes)")
        self._log("Press Serve && Show QR to send it to the phone.")

    def _inspect_profile(self, data: bytes) -> dict:
        """
        Pull display name / identifier / DNS details out of a .mobileconfig.
        Handles both plain XML plists and CMS-wrapped signed profiles — for
        signed, strips the wrapper and parses the payload. Any field we can't
        find just comes back missing; the raw bytes still get served fine.
        """
        try:
            import plistlib
            # Try plain plist first
            try:
                top = plistlib.loads(data)
            except Exception:
                # Signed/CMS wrapped: extract the inner XML plist
                start = data.find(b"<?xml")
                end = data.find(b"</plist>")
                if start == -1 or end == -1:
                    return {}
                top = plistlib.loads(data[start:end + len(b"</plist>")])

            out = {
                "display": top.get("PayloadDisplayName", ""),
                "identifier": top.get("PayloadIdentifier", ""),
            }
            for entry in top.get("PayloadContent", []) or []:
                if entry.get("PayloadType") == "com.apple.dnsSettings.managed":
                    ds = entry.get("DNSSettings", {}) or {}
                    out["protocol"] = ds.get("DNSProtocol", "")
                    out["server_url"] = ds.get("ServerURL", "")
                    out["server_name"] = ds.get("ServerName", "")
                    out["server_addresses"] = list(ds.get("ServerAddresses", []) or [])
                    break
            return out
        except Exception as e:
            self._log(f"[!] Could not introspect profile: {e}")
            return {}

    # ---------- actions ----------

    def _save_profile(self):
        try:
            data = self._payload()
        except Exception as e:
            QMessageBox.warning(self, "iSideload", str(e)); return
        name = self.name_edit.text().strip() or "dns"
        suggested = f"{name}.mobileconfig".replace(" ", "_")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save .mobileconfig", suggested, "Profiles (*.mobileconfig)"
        )
        if not path:
            return
        Path(path).write_bytes(data)
        self._log(f"Saved: {path}")

    def _serve(self):
        try:
            data = self._payload()
        except Exception as e:
            QMessageBox.warning(self, "iSideload", str(e)); return

        self._stop()  # drop any prior server

        safe_name = (self.name_edit.text().strip() or "iSideload-DNS").replace(" ", "_")
        self.server = ProfileServer(
            payload=data,
            filename=f"{safe_name}.mobileconfig",
            max_downloads=3,
            lifetime_sec=300,
            on_download=lambda m: self._log(m),
        )
        self.server.start()

        pm = _pixmap_from_qr(self.server.url, box_size=10)
        self.qr_label.setPixmap(pm.scaled(
            self.qr_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))
        self.url_label.setText(f"Server URL: {self.server.url}")
        self.stop_btn.setEnabled(True)
        self.serve_btn.setEnabled(False)
        self._log(f"Serving on {self.server.url}")

    def _install_usb(self):
        try:
            data = self._payload()
        except Exception as e:
            QMessageBox.warning(self, "iSideload", str(e)); return

        try:
            devs = list_devices()
        except DeviceError as e:
            QMessageBox.critical(self, "iSideload", str(e)); return

        if not devs:
            QMessageBox.warning(
                self, "iSideload",
                "No iPhone detected over USB. Plug one in, tap Trust on the phone, "
                "then try again."
            )
            return

        if len(devs) == 1:
            dev = devs[0]
        else:
            # Multiple devices: pick the first one but tell the user.
            dev = devs[0]
            self._log(f"Multiple devices present; using {dev.name} ({dev.udid[:8]}…)")

        self.usb_btn.setEnabled(False)
        self._log(f"Installing to {dev.name} ({dev.udid[:8]}…) over USB…")

        self._usb_thread = _UsbInstallThread(dev.udid, data)
        self._usb_thread.log_line.connect(self._log)
        self._usb_thread.done.connect(self._on_usb_done)
        self._usb_thread.failed.connect(self._on_usb_failed)
        self._usb_thread.start()

    def _on_usb_done(self):
        self.usb_btn.setEnabled(True)
        QMessageBox.information(
            self, "iSideload",
            "Profile pushed to the phone.\n\n"
            "Now open Settings → General → VPN & Device Management → "
            "Downloaded Profile → Install on the iPhone."
        )

    def _on_usb_failed(self, msg: str):
        self.usb_btn.setEnabled(True)
        self._log(f"[!] USB install failed: {msg}")
        QMessageBox.critical(self, "iSideload", f"USB install failed:\n\n{msg}")

    def _stop(self):
        if self.server is not None:
            self.server.stop()
            self._log("Server stopped.")
            self.server = None
        self.stop_btn.setEnabled(False)
        self.serve_btn.setEnabled(True)
        self.url_label.setText("Server URL: (not running)")
        self.timer_label.setText("Timer: —")
        self.qr_label.clear()
        self.qr_label.setText("QR appears here\nafter Serve")

    def _tick(self):
        if self.server is None:
            return
        if not self.server.is_running():
            self._log("Server auto-stopped (timeout or download cap reached).")
            self._stop()
            return
        self.timer_label.setText(f"Timer: {self.server.time_left()}s left")

    def closeEvent(self, e):
        self._stop()
        super().closeEvent(e)
