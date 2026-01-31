from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


def default_adb_candidates() -> list[str]:
    candidates: list[str] = []

    # 1) On PATH
    candidates.append("adb")

    # 2) Common SDK locations (Windows)
    local = os.environ.get("LOCALAPPDATA")
    user = os.environ.get("USERPROFILE")
    if local:
        candidates.append(str(Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe"))
    if user:
        candidates.append(str(Path(user) / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe"))

    # 3) Fallback common installs
    candidates.extend(
        [
            r"C:\Android\platform-tools\adb.exe",
            r"C:\Program Files\Android\Android Studio\platform-tools\adb.exe",
            r"C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe",
        ]
    )

    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def resolve_adb(adb_arg: str | None) -> str:
    if adb_arg:
        return adb_arg
    for cand in default_adb_candidates():
        try:
            proc = subprocess.run([cand, "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if proc.returncode == 0:
                return cand
        except FileNotFoundError:
            continue
    raise SystemExit("adb not found. Pass --adb <path-to-adb.exe> or add platform-tools to PATH.")


def run_adb(adb: str, args: list[str], timeout_s: float) -> tuple[int, str, str]:
    proc = subprocess.run(
        [adb, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def adb_shell(adb: str, serial: str | None, args: list[str], timeout_s: float) -> tuple[int, str, str]:
    base = ["-s", serial] if serial else []
    return run_adb(adb, [*base, "shell", *args], timeout_s=timeout_s)


def adb_exec_out(adb: str, serial: str | None, args: list[str], timeout_s: float) -> tuple[int, bytes, str]:
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["exec-out", *args]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_s)
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", errors="replace")


def list_devices(adb: str, timeout_s: float) -> list[tuple[str, str]]:
    """Return [(serial, state)] from `adb devices` (state is usually 'device', 'offline', 'unauthorized')."""
    rc, out, err = run_adb(adb, ["devices"], timeout_s=timeout_s)
    if rc != 0:
        raise RuntimeError(f"adb devices failed: {err.strip()}")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    devices: list[tuple[str, str]] = []
    for ln in lines:
        if ln.lower().startswith("list of devices"):
            continue
        parts = ln.split()
        if len(parts) < 2:
            continue
        devices.append((parts[0], parts[1]))
    return devices


def pick_default_serial(adb: str, timeout_s: float) -> str | None:
    devices = [(s, st) for s, st in list_devices(adb, timeout_s=timeout_s) if st == "device"]
    if not devices:
        return None
    if len(devices) == 1:
        return devices[0][0]
    # Prefer Wi‑Fi wireless debugging TLS connect record
    for s, _ in devices:
        if "_adb-tls-connect._tcp" in s:
            return s
    return devices[0][0]


def ensure_device_ready(adb: str, serial: str | None, timeout_s: float) -> None:
    base = ["-s", serial] if serial else []

    rc, out, _ = run_adb(adb, [*base, "get-state"], timeout_s=timeout_s)
    if rc == 0 and out.strip() == "device":
        return

    run_adb(adb, ["start-server"], timeout_s=timeout_s)

    t0 = time.time()
    while time.time() - t0 < timeout_s:
        rc, out, err = run_adb(adb, [*base, "get-state"], timeout_s=timeout_s)
        if rc == 0 and out.strip() == "device":
            return
        if "unauthorized" in (out + err):
            raise SystemExit("Device unauthorized. Please accept the USB debugging prompt on the phone.")
        time.sleep(1.0)

    # Second-chance recovery
    run_adb(adb, ["kill-server"], timeout_s=timeout_s)
    run_adb(adb, ["start-server"], timeout_s=timeout_s)
    rc, out, _ = run_adb(adb, [*base, "get-state"], timeout_s=timeout_s)
    if rc == 0 and out.strip() == "device":
        return

    raise TimeoutError("Device not ready (timeout)")


@dataclass(frozen=True)
class ShellResult:
    rc: int
    out: str
    err: str


def shell_ok(adb: str, serial: str | None, args: list[str], timeout_s: float) -> str:
    rc, out, err = adb_shell(adb, serial, args, timeout_s=timeout_s)
    if rc != 0:
        raise RuntimeError((err or out).strip() or f"adb shell failed: {' '.join(args)}")
    return out
