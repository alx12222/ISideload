# iSideload

**Sign and install iOS apps on your iPhone from Windows. Over USB. No iTunes, no Apple ID, no sideloading service.**

iSideload is a Windows GUI tool that re-signs `.ipa` files with your own certificate and installs them straight to an iPhone, iPad, or iPod over USB. Drop in a `.p12` + password + `.mobileprovision`, pick an IPA, hit Sign & Install — the app lands on the device's home screen a few seconds later.

![Windows](https://img.shields.io/badge/Windows-10%2F11-blue) ![Python](https://img.shields.io/badge/Python-3.12-yellow) ![PyQt6](https://img.shields.io/badge/PyQt6-6.6%2B-green)

---

## What it does

- **Re-signs IPAs with zsign** (bundled `zsign.exe`, same signer as the Linux/macOS version).
- **Installs over USB** via `ideviceinstaller` / `pymobiledevice3` — no WiFi, no cloud.
- **Auto-trusts** the certificate on-device after install so the app launches without the "Untrusted Developer" dialog. *(For profiles that support it — enterprise/ad-hoc.)*
- **Pushes anti-revoker DNS profiles** to the device over USB or via a QR code into Safari.
- **Watches your `certs/` folder** and auto-imports `.p12` / `.mobileprovision` / `.zip` bundles the moment they appear.
- **Built-in cert browser** — a WebView you can point at any sideloading-cert site. Download a cert; iSideload imports it automatically.
- **Auto-fills passwords** from sibling `.txt` / `.password` files next to the `.p12`.

---

## How it works

iSideload is a thin Windows GUI (PyQt6) wrapping two things that already work:

- **`zsign.exe`** — re-signs the `.ipa` with your identity. Vendored and built from source (see *Building* below) so there's no runtime dependency on `zsign` being on PATH.
- **libimobiledevice / pymobiledevice3** — talks to the connected iPhone over USB using Apple's `usbmuxd` protocol. Used for device detection, IPA install, provisioning-profile push, and the auto-trust pipeline.

The install flow is:

1. Run `zsign -k <p12> -p <password> -m <profile> -o <signed.ipa> <input.ipa>`.
2. Push the matching `.mobileprovision` to the device via `MisagentService` (so trust works).
3. `ideviceinstaller install <signed.ipa>`.
4. Poll `InstallationProxyService.get_apps()` until the app shows up.
5. Launch it via `DvtProvider` + `ProcessControl` (which mounts the Developer Disk Image if needed) — this triggers iOS's trust-approval UI automatically.

---

## Setup

### 1. Install Python 3.12

PyQt6 doesn't have 3.14 wheels yet. Get 3.12 from [python.org](https://www.python.org/downloads/release/python-3120/).

```powershell
py -3.12 -m pip install --upgrade pip
```

### 2. Clone and install dependencies

```powershel
```

`requirements.txt` includes `PyQt6`, `PyQt6-WebEngine`, `pymobiledevice3`, `qrcode[pil]`.

### 3. Grab libimobiledevice binaries

Download [libimobiledevice-win32](https://github.com/libimobiledevice-win32/imobiledevice-net/releases) and drop these into `tools/`:

- `idevice_id.exe`
- `ideviceinfo.exe`
- `ideviceinstaller.exe`
- (plus the DLLs they ship with)

### 4. Build `zsign.exe` (one-time)

See *Building zsign* below. The pre-built binary goes into `tools/zsign.exe`.

### 5. Install iTunes or Apple Mobile Device Support

Windows needs Apple's USB drivers to see your iPhone. Install iTunes from Apple's site (not the Microsoft Store version — the Store version's driver install is unreliable), then unplug and replug the device.

### 6. Run

```powershell
py -3.12 main.py
```

---

## Using iSideload

1. **Plug in your iPhone** and tap "Trust" on the device if prompted.
2. **Verify connection:** the top bar should show your device name and UDID.
3. **Drop a cert bundle into the `certs/` folder.** iSideload auto-imports it. Or use the Import button.
4. **Pick an IPA** from the file picker.
5. **Enter the cert password** (or let iSideload auto-fill it from a sibling `.txt`).
6. **Click Sign & Install.**

The status pane shows each step. On success, the app appears on the home screen — tap it. If auto-trust succeeded, it launches directly. If not, go to *Settings → General → VPN & Device Management → trust the cert*.

---

## Cert browser

The "Browse Certificates" button opens a built-in web browser pointed at a configurable URL. Download `.p12` / `.mobileprovision` files from there — iSideload captures the download and imports them into `certs/` automatically.

The default URL is blank; set it to your preferred cert source in Settings.

---

## DNS profiles

**What this is:** anti-revoker DNS profiles redirect Apple's OCSP (certificate revocation check) domains to a sinkhole, so iOS can't phone home to check if your enterprise cert is revoked. If your cert is on CRLite (iOS's on-device blocklist), DNS won't help. If it's revoked only via OCSP, DNS does.

**Install options:**
- **USB** (recommended) — the profile installs silently via `MobileConfigService`.
- **QR code** — iSideload spins up a local HTTP server, encodes its URL as a QR, and you scan it with the iPhone camera. Safari opens, prompts to download the profile, then Settings prompts to install it.

Twelve preset profiles are built in. You can also import your own `.mobileconfig`.

---

## Building zsign

Zsign is C++ + OpenSSL. On Windows, it needs Visual Studio 2022 and OpenSSL. The source repo ships a `build/windows/vs2022/zsign.vcxproj`:

```powershell
git clone https://github.com/zhlynn/zsign
cd zsign
# Install OpenSSL for Windows via vcpkg or Shining Light's installer.
# Open build\windows\vs2022\zsign.sln in VS 2022, set x64 Release, Build.
# Copy build\windows\vs2022\zsign\x64\Release\zsign.exe into iSideload\tools\
```

If you hit `LNK2005` on base64 symbols, check the vcxproj for a duplicate `common/base64.cpp` entry and remove it.

---

## Troubleshooting

**"idevice_id.exe missing"**
Your `tools/` folder is empty or on the wrong path. Drop the libimobiledevice binaries into `iSideload/tools/`.

**"zsign not found"**
Build `zsign.exe` (above) and put it in `tools/zsign.exe`.

**Device doesn't show up in the top bar**
Install iTunes for the drivers, then unplug and replug. If still nothing, run `tools\idevice_id.exe -l` from PowerShell — if it prints nothing, the OS isn't seeing the device as an Apple one.

**"0xe8008018 ApplicationVerificationFailed" during install**
Your certificate has been revoked. iOS checked its on-device CRLite blocklist and rejected it. Get a new cert; DNS tricks won't save you from CRLite.

**"Install: VerifyingApplication (40%)"** that gets stuck
Usually the `.mobileprovision` isn't on the device yet. iSideload pushes it before install, but if it was pushed earlier and expired, open *Settings → General → VPN & Device Management* and delete stale profiles.

**PyQt6 fails to install on Python 3.14**
PyQt6 doesn't ship 3.14 wheels. Use Python 3.12.

**"Pairing dialog response pending" forever**
Tap "Trust" on the iPhone. If the prompt never appears, unpair via `tools\idevicepair.exe unpair` and replug.

---

## What this isn't

- Not a jailbreak and not a cert broker. You bring your own `.p12`.
- Not a way to install revoked apps. If iOS's CRLite blocklist has your cert, no tool can bypass it.
- Not a replacement for Xcode. It installs what you give it; it doesn't build.
- Not iTunes. There's no sync, no backup, no media library. It just signs and installs IPAs.

---

## Project layout

```
isideload/
├── main.py                    # entry point
├── gui.py                     # PyQt6 main window
├── signer.py                  # zsign wrapper
├── device.py                  # libimobiledevice + pymobiledevice3 glue
├── dns_dialog.py              # anti-revoker DNS profile UI
├── vault_browser.py           # built-in cert browser WebView
├── tools/
│   ├── zsign.exe              # bundled signer (you build this)
│   ├── idevice_id.exe         # libimobiledevice
│   ├── ideviceinfo.exe
│   ├── ideviceinstaller.exe
│   └── *.dll
├── certs/                     # auto-imported cert bundles live here
└── requirements.txt
```



## License

GPL-3.0 (inherited from zsign). The Python code is MIT-compatible, but shipping zsign makes the whole package GPL.

---

## Sister project

If you want to do all of this from the iPhone itself with no PC at all, check out [**SignX**](../signx) — same idea, native iOS app, same zsign under the hood.
