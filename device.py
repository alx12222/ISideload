"""
iPhone enumeration and IPA install. Wraps libimobiledevice binaries that
ship in the Windows build:
  - idevice_id.exe       list connected UDIDs
  - ideviceinfo.exe      pull device name / iOS version
  - ideviceinstaller.exe install a signed IPA

Grab the Windows build from:
  https://github.com/libimobiledevice-win32/imobiledevice-net/releases
Drop the .exe + DLLs into: <app_dir>/tools/libimobiledevice/
"""

import os
import sys
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class Device:
    udid: str
    name: str
    product_type: str
    ios_version: str


class DeviceError(Exception):
    pass


def _tool_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "tools" / "libimobiledevice"


def _bin(name: str) -> Path:
    exe = _tool_dir() / (f"{name}.exe" if os.name == "nt" else name)
    if not exe.exists():
        raise DeviceError(
            f"{exe.name} missing at {exe}. Install libimobiledevice-win32 into tools/libimobiledevice/."
        )
    return exe


def _run(cmd: List[str], timeout: int = 60) -> str:
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise DeviceError(f"Timed out: {' '.join(cmd)}") from e

    if res.returncode != 0:
        msg = (res.stderr or res.stdout or "").strip()
        raise DeviceError(f"{Path(cmd[0]).name} exit {res.returncode}: {msg}")
    return res.stdout


def list_devices() -> List[Device]:
    idevice_id = _bin("idevice_id")
    ideviceinfo = _bin("ideviceinfo")

    out = _run([str(idevice_id), "-l"]).strip()
    if not out:
        return []

    devices: List[Device] = []
    for udid in out.splitlines():
        udid = udid.strip()
        if not udid:
            continue
        try:
            name = _run([str(ideviceinfo), "-u", udid, "-k", "DeviceName"]).strip()
            product = _run([str(ideviceinfo), "-u", udid, "-k", "ProductType"]).strip()
            version = _run([str(ideviceinfo), "-u", udid, "-k", "ProductVersion"]).strip()
        except DeviceError:
            # Device is paired-but-locked or not trusted yet.
            name, product, version = "(locked / trust required)", "?", "?"
        devices.append(Device(udid=udid, name=name, product_type=product, ios_version=version))
    return devices


def install_profile_usb(
    udid: str,
    profile_bytes: bytes,
    log: Callable[[str], None],
) -> list:
    """
    Push a .mobileconfig to the device over USB via com.apple.mobile.MCInstall.
    Returns the device's profile list AFTER the push, so the caller can verify
    the profile actually reached iOS (as installed or pending user approval).

    pymobiledevice3 is async, so this wraps the full flow in asyncio.run.
    """
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.mobile_config import MobileConfigService
    except ImportError as e:
        raise DeviceError(
            "pymobiledevice3 is not installed. Run:\n"
            "  py -3.12 -m pip install pymobiledevice3\n"
            f"({e})"
        ) from e

    import asyncio

    async def _do() -> list:
        log(f"Opening lockdown to {udid[:8]}…")
        lockdown = await create_using_usbmux(serial=udid)
        try:
            mc = MobileConfigService(lockdown=lockdown)
            log("Listing current profiles (before)…")
            try:
                before = await mc.get_profile_list()
                before_ids = _profile_ids(before)
                log(f"  found {len(before_ids)} profile(s): {before_ids or '[]'}")
            except Exception as e:
                before_ids = []
                log(f"  (could not list: {e})")

            log(f"Pushing profile ({len(profile_bytes)} bytes) via MCInstall…")
            try:
                resp = await mc.install_profile(profile_bytes)
                log(f"  MCInstall response: {resp!r}")
            except Exception as e:
                raise DeviceError(f"MCInstall rejected the profile: {e}") from e

            log("Listing profiles (after)…")
            try:
                after = await mc.get_profile_list()
                after_ids = _profile_ids(after)
                log(f"  found {len(after_ids)} profile(s): {after_ids or '[]'}")
            except Exception as e:
                after_ids = []
                log(f"  (could not list: {e})")

            new = [i for i in after_ids if i not in before_ids]
            if new:
                log(f"[+] New on device: {new}")
            else:
                log("[!] No new profile detected in the device list. Likely "
                    "pending user approval, or MCInstall accepted bytes but "
                    "iOS held it back (signature/policy).")
            return after_ids
        finally:
            try:
                await lockdown.close()
            except Exception:
                try:
                    lockdown.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_do())
    except DeviceError:
        raise
    except Exception as e:
        raise DeviceError(f"USB install failed: {e}") from e


def _profile_ids(profile_list) -> list:
    """
    Extract PayloadIdentifier strings from whatever shape get_profile_list
    returns across pymobiledevice3 versions (dict with ProfileMetadata, or
    a plain list of dicts).
    """
    ids = []
    if profile_list is None:
        return ids
    if isinstance(profile_list, dict):
        meta = profile_list.get("ProfileMetadata") or profile_list.get("OrderedIdentifiers") or {}
        if isinstance(meta, dict):
            ids = list(meta.keys())
        elif isinstance(meta, list):
            ids = list(meta)
    elif isinstance(profile_list, list):
        for p in profile_list:
            if isinstance(p, dict) and "PayloadIdentifier" in p:
                ids.append(p["PayloadIdentifier"])
    return ids


def list_profiles(udid: str) -> list:
    """Return the current profile identifier list on the device."""
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.mobile_config import MobileConfigService
    except ImportError as e:
        raise DeviceError(
            "pymobiledevice3 not installed — pip install pymobiledevice3"
        ) from e

    import asyncio

    async def _do():
        lockdown = await create_using_usbmux(serial=udid)
        try:
            mc = MobileConfigService(lockdown=lockdown)
            return await mc.get_profile_list()
        finally:
            try:
                await lockdown.close()
            except Exception:
                pass

    try:
        raw = asyncio.run(_do())
    except Exception as e:
        raise DeviceError(f"list_profiles failed: {e}") from e
    return _profile_ids(raw)


def install_ipa(
    udid: str,
    ipa_path: str,
    log: Callable[[str], None],
) -> None:
    """
    Push a signed IPA to the device and install it. Fails cleanly if
    the signature doesn't match a provisioning profile the device is
    provisioned for.
    """
    if not Path(ipa_path).is_file():
        raise DeviceError(f"IPA not found: {ipa_path}")

    installer = _bin("ideviceinstaller")
    cmd = [str(installer), "-u", udid, "-i", ipa_path]
    log(f"$ {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip())
    rc = proc.wait()
    if rc != 0:
        raise DeviceError(f"ideviceinstaller failed (exit {rc}).")
    log("[+] install complete")


# ---------- provisioning profile push (misagent) ----------

def push_provisioning_profile(
    udid: str,
    mobileprovision_path: str,
    log: Callable[[str], None],
) -> None:
    """
    Install a .mobileprovision onto the device via com.apple.misagent.
    For a dev-signed IPA this eliminates the Trust tap entirely — misagent-
    installed profiles ARE the trust mechanism iOS uses for dev certs.

    For enterprise certs it preloads the profile so the one-time Trust tap
    in Settings → VPN & Device Management is the only user interaction
    remaining, and every subsequent IPA signed by the same team installs
    + launches without touching Settings.
    """
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.misagent import MisagentService
    except ImportError as e:
        raise DeviceError(
            "pymobiledevice3 missing (pip install pymobiledevice3)."
        ) from e

    import asyncio

    data = Path(mobileprovision_path).read_bytes()
    log(f"Reading provision: {mobileprovision_path} ({len(data)} bytes)")

    async def _do():
        lockdown = await create_using_usbmux(serial=udid)
        try:
            svc = MisagentService(lockdown=lockdown)
            log("Pushing provisioning profile via com.apple.misagent…")
            resp = await svc.install(data)
            log(f"  misagent response: {resp!r}")
        finally:
            try:
                await lockdown.close()
            except Exception:
                pass

    try:
        asyncio.run(_do())
        log("[+] provisioning profile installed.")
    except Exception as e:
        raise DeviceError(f"misagent install failed: {e}") from e


# ---------- bundle-id extraction from IPA ----------

def extract_bundle_id(ipa_path: str) -> Optional[str]:
    """Pull CFBundleIdentifier out of an IPA's Info.plist without unzipping fully."""
    import zipfile
    import plistlib
    try:
        with zipfile.ZipFile(ipa_path) as z:
            # Find Payload/<Something>.app/Info.plist
            for name in z.namelist():
                parts = name.split("/")
                if (len(parts) == 3
                        and parts[0] == "Payload"
                        and parts[1].endswith(".app")
                        and parts[2] == "Info.plist"):
                    with z.open(name) as f:
                        plist = plistlib.load(f)
                        return plist.get("CFBundleIdentifier")
    except Exception:
        return None
    return None


# ---------- trust-state probe + auto-launch ----------

def wait_for_trust_and_launch(
    udid: str,
    bundle_id: str,
    log: Callable[[str], None],
    timeout_sec: int = 300,
    poll_interval_sec: float = 1.0,
) -> bool:
    """
    After an IPA is installed, poll the device for trust state and launch
    the app the instant trust lands. Returns True on launch, False on timeout.

    How it works: the installation_proxy lists installed apps. An app that
    is installed but not-yet-trusted has an IsUntrusted=True (or similar)
    flag on it. When the user taps Trust in Settings for the signing team,
    that flag flips and the app becomes launchable. We poll for the flip,
    then invoke instproxy's launch via the house arrest / lldb debugserver
    bridge.
    """
    try:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
    except ImportError as e:
        raise DeviceError("pymobiledevice3 missing.") from e

    import asyncio, time

    async def _poll_once(lockdown) -> tuple[bool, Optional[dict]]:
        """Return (is_trusted_and_launchable, raw_app_record)."""
        proxy = InstallationProxyService(lockdown=lockdown)
        apps = await proxy.get_apps(application_type="User")
        if isinstance(apps, list):
            record = next((a for a in apps
                           if isinstance(a, dict)
                           and a.get("CFBundleIdentifier") == bundle_id), None)
        elif isinstance(apps, dict):
            record = apps.get(bundle_id)
        else:
            record = None
        if record is None:
            return (False, None)
        # Different iOS versions use different keys to mark untrusted state.
        untrusted = (
            record.get("IsUntrusted") is True
            or record.get("EnterpriseUpgrade") is True
            or record.get("Entitlements", {}).get("com.apple.developer.restricted-resource-mode") is not None
            or record.get("ApplicationType") == "User" and record.get("SignerIdentity") is None
        )
        return (not untrusted, record)

    async def _ensure_ddi(lockdown):
        """
        Mount the Developer Disk Image if it isn't already. iOS gates the
        DVT lockdown service behind this — without a DDI, the service is
        'not offered'. For iOS <17 this is a small (~5MB) download of the
        public Apple DDI matching the device's OS version.
        """
        try:
            from pymobiledevice3.services.mobile_image_mounter import (
                MobileImageMounterService, auto_mount_developer,
            )
            mim = MobileImageMounterService(lockdown=lockdown)
            if mim.is_image_mounted("Developer"):
                return True
            log("Mounting Developer Disk Image (one-time per reboot)…")
            await auto_mount_developer(lockdown=lockdown)
            log("[+] DDI mounted.")
            return True
        except Exception as e:
            log(f"  DDI mount failed ({e})")
            return False

    async def _launch(lockdown):
        """Launch the app via DTX instruments (same pipe Xcode uses)."""
        # DTX service is only exposed when a Developer Disk Image is mounted.
        await _ensure_ddi(lockdown)
        try:
            from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
            from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
            # DvtProvider is the concrete subclass with the right SERVICE_NAME
            # ('com.apple.instruments.remoteserver.DVTSecureSocketProxy').
            provider = DvtProvider(lockdown=lockdown)
            await provider.connect()
            try:
                pc = ProcessControl(provider)
                pid = pc.launch(bundle_id=bundle_id, kill_existing=True)
                log(f"[+] Launched {bundle_id} (pid={pid})")
            finally:
                try:
                    await provider.close()
                except Exception:
                    pass
            return
        except Exception as e:
            log(f"  DTX launch failed ({e}); open the app from the home screen.")

    async def _run():
        lockdown = await create_using_usbmux(serial=udid)
        try:
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                trusted, record = await _poll_once(lockdown)
                if record is None:
                    log(f"  waiting for install to settle…")
                elif trusted:
                    log(f"[+] Trust landed for {bundle_id}. Launching…")
                    await _launch(lockdown)
                    return True
                else:
                    secs_left = int(deadline - time.time())
                    log(f"  waiting for Trust tap… ({secs_left}s)")
                await asyncio.sleep(poll_interval_sec)
            log("[!] Timed out waiting for trust. Tap Trust in Settings whenever ready.")
            return False
        finally:
            try:
                await lockdown.close()
            except Exception:
                pass

    try:
        return asyncio.run(_run())
    except Exception as e:
        raise DeviceError(f"trust-watch failed: {e}") from e
