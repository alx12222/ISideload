"""
Tiny single-shot HTTP server that serves one .mobileconfig to the phone
with the correct MIME type, then self-terminates after a configurable
timeout or N downloads.

Why: Apple only installs .mobileconfig files that arrive through Safari
on the device. We can't push them via libimobiledevice. The phone scans
a QR code pointed at this server and Safari does the rest.
"""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional


MOBILECONFIG_MIME = "application/x-apple-aspen-config"


def _lan_ipv4() -> str:
    """
    Figure out which of the host's LAN IPv4 addresses the phone can reach.
    Doesn't actually send traffic — connect() on UDP just populates the
    local kernel binding.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ProfileServer:
    """
    Serves `payload` (bytes of a .mobileconfig) at /profile.mobileconfig on
    a LAN-visible port. Call .start(), read .url, then .stop() when done
    or let the auto-stop fire.
    """

    def __init__(
        self,
        payload: bytes,
        filename: str = "iSideload-DNS.mobileconfig",
        max_downloads: int = 3,
        lifetime_sec: int = 300,          # 5 min default
        on_download: Optional[Callable[[str], None]] = None,
    ):
        self.payload = payload
        self.filename = filename
        self.max_downloads = max_downloads
        self.lifetime_sec = lifetime_sec
        self.on_download = on_download or (lambda _msg: None)

        self._downloads = 0
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._deadline = 0.0
        self._lock = threading.Lock()

        self.host = _lan_ipv4()
        self.port = _pick_free_port()
        self.url = f"http://{self.host}:{self.port}/{self.filename}"

    def _handler_factory(self):
        payload = self.payload
        filename = self.filename
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # silence default stderr log
                pass

            def do_GET(self):
                # Serve the profile at any path so QR scanners that mangle
                # the URL still work; just require the method to be GET.
                self.send_response(200)
                self.send_header("Content-Type", MOBILECONFIG_MIME)
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

                with parent._lock:
                    parent._downloads += 1
                    hit = parent._downloads
                    peer = self.client_address[0]
                parent.on_download(f"profile served to {peer} (#{hit})")

                if hit >= parent.max_downloads:
                    # Give the TCP side a beat before shutting down.
                    threading.Thread(target=parent._soft_stop, daemon=True).start()

            def do_HEAD(self):
                self.send_response(200)
                self.send_header("Content-Type", MOBILECONFIG_MIME)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()

        return Handler

    def _soft_stop(self):
        time.sleep(0.5)
        self.stop()

    def start(self):
        if self._server is not None:
            return
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), self._handler_factory())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._deadline = time.time() + self.lifetime_sec

        # Watchdog: auto-stop at lifetime.
        def watchdog():
            while self._server is not None and time.time() < self._deadline:
                time.sleep(0.5)
            self.stop()

        threading.Thread(target=watchdog, daemon=True).start()

    def stop(self):
        srv = self._server
        self._server = None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass

    def is_running(self) -> bool:
        return self._server is not None

    def time_left(self) -> int:
        return max(0, int(self._deadline - time.time()))
