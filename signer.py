"""
IPA signing backend. Wraps zsign.exe to re-sign an IPA with a user-provided
P12 certificate and mobileprovision file.

zsign reference: https://github.com/zhlynn/zsign
Expected on disk at: <app_dir>/tools/zsign.exe
"""

import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class SignJob:
    ipa_path: str
    p12_path: str
    p12_password: str
    mobileprovision_path: str
    output_path: str
    bundle_id: Optional[str] = None       # optional override
    bundle_name: Optional[str] = None     # optional override
    bundle_version: Optional[str] = None  # optional override


class SignError(Exception):
    pass


def _tool_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "tools"


def zsign_path() -> Path:
    exe = _tool_dir() / ("zsign.exe" if os.name == "nt" else "zsign")
    if not exe.exists():
        raise SignError(
            f"zsign not found at {exe}. Download the Windows build from "
            "https://github.com/zhlynn/zsign/releases and drop zsign.exe into the tools/ folder."
        )
    return exe


def validate_inputs(job: SignJob) -> None:
    for label, p in (
        ("IPA", job.ipa_path),
        ("P12 certificate", job.p12_path),
        ("mobileprovision", job.mobileprovision_path),
    ):
        if not p or not Path(p).is_file():
            raise SignError(f"{label} path is missing or not a file: {p}")

    if Path(job.ipa_path).suffix.lower() != ".ipa":
        raise SignError("IPA input must have a .ipa extension.")
    if Path(job.p12_path).suffix.lower() not in (".p12", ".pfx"):
        raise SignError("Certificate must be a .p12 or .pfx file.")
    if Path(job.mobileprovision_path).suffix.lower() != ".mobileprovision":
        raise SignError("Provisioning profile must be a .mobileprovision file.")


def sign_ipa(job: SignJob, log: Callable[[str], None]) -> str:
    """
    Run zsign against the job inputs. Returns the output IPA path.
    log(msg) is called with progress lines.
    """
    validate_inputs(job)
    exe = zsign_path()

    out = Path(job.output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    # zsign args:
    #   -k <p12>        signing certificate
    #   -p <password>   p12 password
    #   -m <mp>         mobileprovision
    #   -o <out.ipa>    output
    #   -z 5            compression level (speed/size tradeoff)
    #   -f              force rewrite
    #   [-b bundleid] [-n name] [-v version]
    cmd = [
        str(exe),
        "-k", job.p12_path,
        "-p", job.p12_password,
        "-m", job.mobileprovision_path,
        "-o", str(out),
        "-z", "5",
        "-f",
    ]
    if job.bundle_id:
        cmd += ["-b", job.bundle_id]
    if job.bundle_name:
        cmd += ["-n", job.bundle_name]
    if job.bundle_version:
        cmd += ["-v", job.bundle_version]
    cmd.append(job.ipa_path)

    # Mask the password in the logged command
    safe = list(cmd)
    if "-p" in safe:
        idx = safe.index("-p")
        safe[idx + 1] = "********"
    log(f"$ {' '.join(safe)}")

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
        raise SignError(f"zsign failed (exit {rc}). See log for details.")

    if not out.exists():
        raise SignError("zsign reported success but output IPA is missing.")

    log(f"[+] signed IPA: {out}")
    return str(out)


def inspect_provision(mobileprovision_path: str) -> dict:
    """
    Pull a quick summary out of a .mobileprovision file. Pure-python,
    no external deps: strip the CMS wrapper and parse the plist payload.
    """
    import plistlib
    import re

    data = Path(mobileprovision_path).read_bytes()
    start = data.find(b"<?xml")
    end = data.find(b"</plist>")
    if start == -1 or end == -1:
        raise SignError("Unable to locate plist payload in mobileprovision.")
    pl = plistlib.loads(data[start:end + len(b"</plist>")])

    summary = {
        "AppIDName": pl.get("AppIDName"),
        "TeamName": pl.get("TeamName"),
        "TeamIdentifier": pl.get("TeamIdentifier"),
        "ExpirationDate": str(pl.get("ExpirationDate")),
        "ProvisionedDevices": len(pl.get("ProvisionedDevices") or []),
        "Entitlements_app_id": (pl.get("Entitlements") or {}).get("application-identifier"),
        "Type": "AdHoc" if pl.get("ProvisionedDevices") else ("Enterprise" if pl.get("ProvisionsAllDevices") else "Development/AppStore"),
    }
    return summary
