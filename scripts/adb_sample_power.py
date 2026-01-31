from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _default_adb_candidates() -> list[str]:
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

    # De-dup preserve order
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _resolve_adb(adb_arg: str | None) -> str:
    if adb_arg:
        return adb_arg
    for cand in _default_adb_candidates():
        try:
            proc = subprocess.run([cand, "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if proc.returncode == 0:
                return cand
        except FileNotFoundError:
            continue
    raise SystemExit("adb not found. Pass --adb <path-to-adb.exe> or add platform-tools to PATH.")


@dataclass
class BatteryReading:
    level: int | None
    scale: int | None
    voltage_mv: int | None
    temp_deci_c: int | None
    charge_counter_uah: int | None
    status: int | None
    plugged: int | None
    ac_powered: int | None
    usb_powered: int | None
    wireless_powered: int | None
    raw_updates_stopped: bool


_BATT_KV = {
    "level": re.compile(r"^\s*level:\s*(\d+)\s*$", re.MULTILINE),
    "scale": re.compile(r"^\s*scale:\s*(\d+)\s*$", re.MULTILINE),
    "voltage": re.compile(r"^\s*voltage:\s*(\d+)\s*$", re.MULTILINE),
    "temperature": re.compile(r"^\s*temperature:\s*(\d+)\s*$", re.MULTILINE),
    "charge_counter": re.compile(r"^\s*Charge counter:\s*(\d+)\s*$", re.MULTILINE),
    "status": re.compile(r"^\s*status:\s*(\d+)\s*$", re.MULTILINE),
    "plugged": re.compile(r"^\s*plugged:\s*(\d+)\s*$", re.MULTILINE),
    "ac_powered": re.compile(r"^\s*AC powered:\s*(true|false)\s*$", re.MULTILINE | re.IGNORECASE),
    "usb_powered": re.compile(r"^\s*USB powered:\s*(true|false)\s*$", re.MULTILINE | re.IGNORECASE),
    "wireless_powered": re.compile(r"^\s*Wireless powered:\s*(true|false)\s*$", re.MULTILINE | re.IGNORECASE),
}


def _parse_bool_as_int(regex: re.Pattern[str], text: str) -> int | None:
    m = regex.search(text)
    if not m:
        return None
    v = (m.group(1) or "").strip().lower()
    if v == "true":
        return 1
    if v == "false":
        return 0
    return None


@dataclass
class BatteryPropertiesReading:
    current_now_uA: int | None
    current_average_uA: int | None
    energy_counter: int | None
    charge_counter_uAh: int | None


_BPROPS_KV = {
    # Various formats observed across Android/OEM builds:
    # - current_now: -123456
    # - currentNow: -123456
    # - mCurrentNow= -123456
    "current_now": re.compile(r"(?:^\s*(?:current_now|currentNow|CurrentNow)\s*:\s*([-]?\d+)\s*$|mCurrentNow\s*=\s*([-]?\d+))", re.MULTILINE),
    # - current_average: -123456
    # - currentAverage: -123456
    # - mCurrentAverage= -123456
    "current_average": re.compile(
        r"(?:^\s*(?:current_average|currentAverage|CurrentAverage)\s*:\s*([-]?\d+)\s*$|mCurrentAverage\s*=\s*([-]?\d+))",
        re.MULTILINE,
    ),
    # - energy_counter: 123456
    # - energyCounter: 123456
    # - mEnergyCounter= 123456
    "energy_counter": re.compile(
        r"(?:^\s*(?:energy_counter|energyCounter|EnergyCounter)\s*:\s*([-]?\d+)\s*$|mEnergyCounter\s*=\s*([-]?\d+))",
        re.MULTILINE,
    ),
    # Prefer batteryproperties charge counter if present, else keep dumpsys battery Charge counter
    "charge_counter": re.compile(
        r"(?:^\s*(?:charge_counter|chargeCounter|ChargeCounter)\s*:\s*(\d+)\s*$|mChargeCounter\s*=\s*(\d+))",
        re.MULTILINE,
    ),
}


def _parse_int(regex: re.Pattern[str], text: str) -> int | None:
    m = regex.search(text)
    if not m:
        return None
    try:
        # Support multiple capturing groups: return the first non-empty group.
        for i in range(1, (m.lastindex or 0) + 1):
            g = m.group(i)
            if g is None:
                continue
            g = str(g).strip()
            if g:
                return int(g)
        return None
    except Exception:
        return None


def _run(adb: str, args: list[str], timeout_s: float) -> tuple[int, str, str]:
    proc = subprocess.run(
        [adb, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def _list_devices(adb: str, timeout_s: float) -> list[tuple[str, str]]:
    """Return [(serial, state)] from `adb devices` (state is usually 'device', 'offline', 'unauthorized')."""
    rc, out, err = _run(adb, ["devices"], timeout_s=timeout_s)
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
        serial, state = parts[0], parts[1]
        devices.append((serial, state))
    return devices


def _sanitize_key(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", str(s).strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return s.lower() if s else "x"


def _sha1_text(text: str) -> str:
    # Keep it stable but avoid pathological memory on huge dumps.
    # dumpsys outputs are usually small; 256KB is plenty for change detection.
    data = text.encode("utf-8", errors="replace")
    if len(data) > 256 * 1024:
        data = data[: 256 * 1024]
    return hashlib.sha1(data).hexdigest()


_RE_BOOL = re.compile(r"\b(true|false)\b", re.IGNORECASE)
_RE_POWER_KV_BOOL = {
    "is_powered": re.compile(r"^\s*mIsPowered=(true|false)\s*$", re.MULTILINE),
    "device_idle": re.compile(r"^\s*mDeviceIdleMode=(true|false)\s*$", re.MULTILINE),
    "light_device_idle": re.compile(r"^\s*mLightDeviceIdleMode=(true|false)\s*$", re.MULTILINE),
    "hal_interactive": re.compile(r"^\s*mHalInteractiveModeEnabled=(true|false)\s*$", re.MULTILINE),
}
_RE_POWER_PLUGTYPE = re.compile(r"^\s*mPlugType=(\d+)\s*$", re.MULTILINE)
_RE_POWER_WAKEFULNESS = re.compile(r"^\s*mWakefulness=(\S+)\s*$", re.MULTILINE)

_RE_SCHEDBOOST_NORMAL = re.compile(r"^\s*currently isNormalPolicy:\s*(true|false)\s*$", re.MULTILINE)
_RE_SCHEDBOOST_UCLAMP_EN = re.compile(r"^\s*ENABLE_RTMODE_UCLAMP:\s*(true|false)\s*$", re.MULTILINE)
_RE_SCHEDBOOST_UCLAMP_MIN = re.compile(r"^\s*TASK_UCLAMP_MIN:\s*(\d+)\s*$", re.MULTILINE)
_RE_SCHEDBOOST_PREBOOST = re.compile(r"^\s*mPreBoostProcessName:\s*(\S+)\s*$", re.MULTILINE)

_RE_WSTONE_AUTOSAVE = re.compile(r"^\s*Global autosave flag:(\d+)\s*$", re.MULTILINE)
_RE_WSTONE_CURRENT_MODE = re.compile(
    r"^>\[(?P<mode>[^\]\s]+)\s+(?P<mode_id>\d+)\]\[(?P<autosave>[^\]]+)\]:Stay for (?P<stay_ms>\d+) ms\((?P<count>\d+) times, average current: (?P<avg_ma>[-]?\d+) mA\)\s*$",
    re.MULTILINE,
)

_RE_PH_PREFERRED_RATE = re.compile(r"^\s*HintSessionPreferredRate:\s*(\d+)\s*$", re.MULTILINE)
_RE_PH_HAL_SUPPORT = re.compile(r"^\s*HAL Support:\s*(true|false)\s*$", re.MULTILINE)
_RE_PH_SESSION_PID = re.compile(r"^\s*SessionPID:\s*(\d+)\s*$", re.MULTILINE)
_RE_PH_SESSION_UID = re.compile(r"^\s*SessionUID:\s*(\d+)\s*$", re.MULTILINE)


def _parse_bool01(maybe_bool: str | None) -> int | str:
    if maybe_bool is None:
        return ""
    v = maybe_bool.strip().lower()
    if v == "true":
        return 1
    if v == "false":
        return 0
    return ""


def _parse_dumpsys_policy_service(service: str, text: str) -> dict[str, object]:
    """Extract a small, stable subset of *explicit* policy state from selected services.

    Goal: a non-guess, low-overhead way to read vendor/framework policy knobs/states.
    """
    svc = service.strip()
    key = _sanitize_key(svc)
    prefix = f"policy_{key}_"

    out: dict[str, object] = {}

    if svc == "SchedBoostService":
        m0 = _RE_SCHEDBOOST_NORMAL.search(text)
        out[prefix + "is_normal_policy"] = _parse_bool01(m0.group(1) if m0 else None)
        m1 = _RE_SCHEDBOOST_UCLAMP_EN.search(text)
        out[prefix + "rtmode_uclamp_enabled"] = _parse_bool01(m1.group(1) if m1 else None)
        mins = [int(m.group(1)) for m in _RE_SCHEDBOOST_UCLAMP_MIN.finditer(text) if m.group(1).isdigit()]
        out[prefix + "task_uclamp_min_a"] = mins[0] if len(mins) >= 1 else ""
        out[prefix + "task_uclamp_min_b"] = mins[1] if len(mins) >= 2 else ""
        m = _RE_SCHEDBOOST_PREBOOST.search(text)
        out[prefix + "preboost_process"] = m.group(1).strip() if m else ""

        # Count list lengths for stability.
        always_rt = 0
        boosting = 0
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if ln.strip() == "AlwaysRtTids:":
                j = i + 1
                while j < len(lines):
                    s = lines[j].strip()
                    if not s:
                        break
                    if s.isdigit():
                        always_rt += 1
                    j += 1
            if ln.strip() == "Boosting Threads:":
                j = i + 1
                while j < len(lines):
                    s = lines[j].strip()
                    if not s:
                        break
                    boosting += 1
                    j += 1
        out[prefix + "always_rt_tids_count"] = always_rt if always_rt > 0 else ""
        out[prefix + "boosting_threads_count"] = boosting if boosting > 0 else ""

    elif svc == "miui.whetstone.power":
        m = _RE_WSTONE_AUTOSAVE.search(text)
        out[prefix + "global_autosave_flag"] = int(m.group(1)) if m and m.group(1).isdigit() else ""
        m2 = _RE_WSTONE_CURRENT_MODE.search(text)
        if m2:
            out[prefix + "mode"] = m2.group("mode")
            out[prefix + "mode_id"] = int(m2.group("mode_id")) if m2.group("mode_id").isdigit() else ""
            out[prefix + "autosave"] = m2.group("autosave")
            out[prefix + "stay_ms"] = int(m2.group("stay_ms")) if m2.group("stay_ms").isdigit() else ""
            out[prefix + "stay_count"] = int(m2.group("count")) if m2.group("count").isdigit() else ""
            try:
                out[prefix + "avg_current_ma"] = int(m2.group("avg_ma"))
            except Exception:
                out[prefix + "avg_current_ma"] = ""

    elif svc == "performance_hint":
        m = _RE_PH_PREFERRED_RATE.search(text)
        out[prefix + "preferred_rate_ns"] = int(m.group(1)) if m and m.group(1).isdigit() else ""
        m = _RE_PH_HAL_SUPPORT.search(text)
        out[prefix + "hal_support"] = _parse_bool01(m.group(1) if m else None)
        pids = [int(m.group(1)) for m in _RE_PH_SESSION_PID.finditer(text) if m.group(1).isdigit()]
        uids = [int(m.group(1)) for m in _RE_PH_SESSION_UID.finditer(text) if m.group(1).isdigit()]
        out[prefix + "active_sessions_count"] = len(pids) if pids else ""
        # Best-effort: first session identifiers.
        out[prefix + "session_pid_0"] = pids[0] if pids else ""
        out[prefix + "session_uid_0"] = uids[0] if uids else ""

    elif svc == "power":
        # Framework power manager state (no vendor guessing)
        for k, rgx in _RE_POWER_KV_BOOL.items():
            m = rgx.search(text)
            out[prefix + k] = _parse_bool01(m.group(1) if m else None)
        m = _RE_POWER_PLUGTYPE.search(text)
        out[prefix + "plug_type"] = int(m.group(1)) if m and m.group(1).isdigit() else ""
        m = _RE_POWER_WAKEFULNESS.search(text)
        out[prefix + "wakefulness"] = m.group(1).strip() if m else ""

    return out


def _policy_service_columns(service: str) -> list[str]:
    svc = service.strip()
    key = _sanitize_key(svc)
    prefix = f"policy_{key}_"
    base = [prefix + "rc", prefix + "sha1"]
    if svc == "SchedBoostService":
        return base + [
            prefix + "is_normal_policy",
            prefix + "rtmode_uclamp_enabled",
            prefix + "task_uclamp_min_a",
            prefix + "task_uclamp_min_b",
            prefix + "preboost_process",
            prefix + "always_rt_tids_count",
            prefix + "boosting_threads_count",
        ]
    if svc == "miui.whetstone.power":
        return base + [
            prefix + "global_autosave_flag",
            prefix + "mode",
            prefix + "mode_id",
            prefix + "autosave",
            prefix + "stay_ms",
            prefix + "stay_count",
            prefix + "avg_current_ma",
        ]
    if svc == "performance_hint":
        return base + [
            prefix + "preferred_rate_ns",
            prefix + "hal_support",
            prefix + "active_sessions_count",
            prefix + "session_pid_0",
            prefix + "session_uid_0",
        ]
    if svc == "power":
        return base + [
            prefix + "is_powered",
            prefix + "plug_type",
            prefix + "wakefulness",
            prefix + "device_idle",
            prefix + "light_device_idle",
            prefix + "hal_interactive",
        ]
    return base


def _pick_default_serial(adb: str, timeout_s: float) -> str | None:
    devices = [(s, st) for s, st in _list_devices(adb, timeout_s=timeout_s) if st == "device"]
    if not devices:
        return None
    if len(devices) == 1:
        return devices[0][0]
    # Prefer Wi‑Fi wireless debugging TLS connect record
    for s, _ in devices:
        if "_adb-tls-connect._tcp" in s:
            return s
    return devices[0][0]


def _ensure_device_ready(adb: str, serial: str | None, timeout_s: float) -> None:
    base = ["-s", serial] if serial else []

    # wait-for-device blocks until connected; but can hang if adb server is stuck.
    # Here we do a gentle check + restart if needed.
    rc, out, _ = _run(adb, [*base, "get-state"], timeout_s=timeout_s)
    if rc == 0 and out.strip() == "device":
        return

    _run(adb, ["start-server"], timeout_s=timeout_s)

    # Short polling loop
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        rc, out, err = _run(adb, [*base, "get-state"], timeout_s=timeout_s)
        if rc == 0 and out.strip() == "device":
            return
        if "unauthorized" in (out + err):
            raise SystemExit("Device unauthorized. Please accept the USB debugging prompt on the phone.")
        time.sleep(1.0)

    # Second-chance recovery
    _run(adb, ["kill-server"], timeout_s=timeout_s)
    _run(adb, ["start-server"], timeout_s=timeout_s)
    rc, out, _ = _run(adb, [*base, "get-state"], timeout_s=timeout_s)
    if rc == 0 and out.strip() == "device":
        return

    raise TimeoutError("Device not ready (timeout)")


def _read_battery(adb: str, serial: str | None, timeout_s: float, auto_reset: bool) -> BatteryReading:
    base = ["-s", serial] if serial else []
    rc, out, err = _run(adb, [*base, "shell", "dumpsys", "battery"], timeout_s=timeout_s)
    if rc != 0:
        raise RuntimeError(f"dumpsys battery failed: {err.strip()}")

    updates_stopped = "UPDATES STOPPED" in out

    if updates_stopped and auto_reset:
        # "假断电"常见表现之一：battery service 停止更新，需 reset
        _run(adb, [*base, "shell", "dumpsys", "battery", "reset"], timeout_s=timeout_s)
        _run(adb, [*base, "shell", "cmd", "battery", "reset"], timeout_s=timeout_s)
        rc, out, err = _run(adb, [*base, "shell", "dumpsys", "battery"], timeout_s=timeout_s)
        if rc != 0:
            raise RuntimeError(f"dumpsys battery failed after reset: {err.strip()}")
        updates_stopped = "UPDATES STOPPED" in out

    return BatteryReading(
        level=_parse_int(_BATT_KV["level"], out),
        scale=_parse_int(_BATT_KV["scale"], out),
        voltage_mv=_parse_int(_BATT_KV["voltage"], out),
        temp_deci_c=_parse_int(_BATT_KV["temperature"], out),
        charge_counter_uah=_parse_int(_BATT_KV["charge_counter"], out),
        status=_parse_int(_BATT_KV["status"], out),
        plugged=_parse_int(_BATT_KV["plugged"], out),
        ac_powered=_parse_bool_as_int(_BATT_KV["ac_powered"], out),
        usb_powered=_parse_bool_as_int(_BATT_KV["usb_powered"], out),
        wireless_powered=_parse_bool_as_int(_BATT_KV["wireless_powered"], out),
        raw_updates_stopped=updates_stopped,
    )


def _read_batteryproperties(adb: str, serial: str | None, timeout_s: float) -> BatteryPropertiesReading:
    """Best-effort higher-frequency battery properties via `dumpsys batteryproperties`.

    On many devices this exposes instantaneous current (uA) which avoids the heavy
    quantization seen in charge_counter updates.
    """
    base = ["-s", serial] if serial else []
    rc, out, err = _run(adb, [*base, "shell", "dumpsys", "batteryproperties"], timeout_s=timeout_s)
    if rc != 0:
        raise RuntimeError(f"dumpsys batteryproperties failed: {err.strip()}")

    return BatteryPropertiesReading(
        current_now_uA=_parse_int(_BPROPS_KV["current_now"], out),
        current_average_uA=_parse_int(_BPROPS_KV["current_average"], out),
        energy_counter=_parse_int(_BPROPS_KV["energy_counter"], out),
        charge_counter_uAh=_parse_int(_BPROPS_KV["charge_counter"], out),
    )


def _read_brightness(adb: str, serial: str | None, timeout_s: float) -> int | None:
    base = ["-s", serial] if serial else []
    rc, out, _ = _run(adb, [*base, "shell", "settings", "get", "system", "screen_brightness"], timeout_s=timeout_s)
    if rc != 0:
        return None
    out = out.strip()
    try:
        return int(out)
    except Exception:
        return None


_RE_DISPLAY_POWER_STATE = re.compile(r"Display Power:\s*state=(\w+)")
_RE_SCREEN_STATE = re.compile(r"\bmScreenState=(\w+)\b")
_RE_DISPLAYDEVICEINFO_STATE = re.compile(r"\bDisplayDeviceInfo\{.*?\bstate\s+(\w+),\s*committedState\s+(\w+)", re.IGNORECASE)


def _read_display_state(adb: str, serial: str | None, timeout_s: float) -> str | None:
    """Best-effort display state from `dumpsys power`.

    Returns one of ON/OFF/DOZE/UNKNOWN-ish strings, or None if unavailable.
    """
    base = ["-s", serial] if serial else []

    # Prefer `dumpsys display` (more stable across OEMs).
    rc, out, _ = _run(adb, [*base, "shell", "dumpsys", "display"], timeout_s=timeout_s)
    if rc == 0:
        m = _RE_SCREEN_STATE.search(out)
        if m:
            return m.group(1)
        m2 = _RE_DISPLAYDEVICEINFO_STATE.search(out)
        if m2:
            return m2.group(1)

    # Fallback: older AOSP format in `dumpsys power`.
    rc, out, _ = _run(adb, [*base, "shell", "dumpsys", "power"], timeout_s=timeout_s)
    if rc != 0:
        return None
    m3 = _RE_DISPLAY_POWER_STATE.search(out)
    if not m3:
        return None
    return m3.group(1)


def _read_time_in_state(adb: str, serial: str | None, policy: int, timeout_s: float) -> dict[int, int] | None:
    base = ["-s", serial] if serial else []
    path = f"/sys/devices/system/cpu/cpufreq/policy{policy}/stats/time_in_state"
    rc, out, err = _run(adb, [*base, "shell", "cat", path], timeout_s=timeout_s)
    if rc != 0:
        if "No such file" in err or "No such file" in out:
            return None
        return None

    times: dict[int, int] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            freq = int(parts[0])
            t = int(parts[1])
        except Exception:
            continue
        times[freq] = t
    return times


def _read_policy_knobs(
    adb: str,
    serial: str | None,
    policies: list[int],
    timeout_s: float,
) -> dict[str, str]:
    """Best-effort read of policy/scheduler knobs (no root required on many builds).

    This is for *detecting* policy changes (boost / min-freq pin / cpuset changes), not for direct power.
    We intentionally do a single adb shell to keep overhead low.
    """
    base = ["-s", serial] if serial else []

    # key -> path (or special marker)
    items: list[tuple[str, str]] = []
    items.append(("cpu_online", "/sys/devices/system/cpu/online"))

    for p in policies:
        items.append((f"cpu_p{p}_scaling_min_freq", f"/sys/devices/system/cpu/cpufreq/policy{p}/scaling_min_freq"))
        items.append((f"cpu_p{p}_scaling_max_freq", f"/sys/devices/system/cpu/cpufreq/policy{p}/scaling_max_freq"))
        items.append((f"cpu_p{p}_scaling_governor", f"/sys/devices/system/cpu/cpufreq/policy{p}/scaling_governor"))

    # Common cpuset groups (existence varies by ROM / cgroup version)
    items.extend(
        [
            ("cpuset_top_app", "/dev/cpuset/top-app/cpus"),
            ("cpuset_foreground", "/dev/cpuset/foreground/cpus"),
            ("cpuset_background", "/dev/cpuset/background/cpus"),
            ("cpuset_system_background", "/dev/cpuset/system-background/cpus"),
        ]
    )

    # Common uclamp knobs (vendor dependent)
    items.extend(
        [
            ("uclamp_top_app_max", "/dev/cpuctl/top-app/uclamp.max"),
            ("uclamp_top_app_min", "/dev/cpuctl/top-app/uclamp.min"),
            ("uclamp_foreground_max", "/dev/cpuctl/foreground/uclamp.max"),
            ("uclamp_foreground_min", "/dev/cpuctl/foreground/uclamp.min"),
        ]
    )

    # Build a single shell command: echo key=value for each path.
    parts: list[str] = []
    for k, path in items:
        # Use POSIX sh, silence errors, strip newlines.
        parts.append(f"v=$(cat {path} 2>/dev/null | tr -d '\\r' | tr -d '\\n'); echo {k}=$v")
    cmd = " ; ".join(parts)
    rc, out, _ = _run(adb, [*base, "shell", "sh", "-c", cmd], timeout_s=timeout_s)
    if rc != 0:
        return {}

    result: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        result[k.strip()] = v.strip()
    return result


_RE_THERMAL_STATUS = re.compile(r"^\s*Thermal Status:\s*(\d+)\s*$", re.MULTILINE)
_RE_THERMAL_TEMP = re.compile(
    r"Temperature\{mValue=(?P<val>[-0-9.]+),\s*mType=(?P<type>\d+),\s*mName=(?P<name>[A-Z0-9_]+),\s*mStatus=(?P<status>\d+)\}"
)


def _read_thermalservice(
    adb: str,
    serial: str | None,
    timeout_s: float,
    want_names: set[str],
) -> dict[str, object]:
    """Read `dumpsys thermalservice` and extract thermal status + selected sensor temperatures.

    Returns a dict with keys:
    - thermal_status (int|None)
    - thermal_<name>_C (float|None) for each requested name
    """
    base = ["-s", serial] if serial else []
    rc, out, _ = _run(adb, [*base, "shell", "dumpsys", "thermalservice"], timeout_s=timeout_s)
    if rc != 0:
        return {}

    result: dict[str, object] = {}

    m = _RE_THERMAL_STATUS.search(out)
    if m:
        try:
            result["thermal_status"] = int(m.group(1))
        except Exception:
            result["thermal_status"] = ""

    # Prefer the "Current temperatures from HAL:" section if present.
    section = out
    marker = "Current temperatures from HAL:"
    idx = out.find(marker)
    if idx >= 0:
        section = out[idx + len(marker) :]
        # stop at next heading
        for stop in ["Current cooling devices", "Temperature static thresholds", "Temperature headroom thresholds"]:
            sidx = section.find(stop)
            if sidx >= 0:
                section = section[:sidx]
                break

    temps: dict[str, float] = {}
    for tm in _RE_THERMAL_TEMP.finditer(section):
        name = tm.group("name")
        if want_names and name not in want_names:
            continue
        try:
            temps[name] = float(tm.group("val"))
        except Exception:
            continue

    for name in sorted(want_names):
        key = f"thermal_{name.lower()}_C"
        result[key] = temps.get(name, "")

    return result


@dataclass
class TimeInStateState:
    last: dict[int, dict[int, int]]  # policy -> (freq->time)


def _delta_time_in_state(state: TimeInStateState, current: dict[int, dict[int, int]]) -> dict[str, int]:
    deltas: dict[str, int] = {}
    for policy, cur_map in current.items():
        prev_map = state.last.get(policy)
        for freq, cur_t in cur_map.items():
            prev_t = prev_map.get(freq, cur_t) if prev_map else cur_t
            d = cur_t - prev_t
            if d < 0:
                # counter reset / wrap
                d = 0
            deltas[f"cpu_p{policy}_freq{freq}_dt"] = d
    state.last = current
    return deltas


def main() -> int:
    parser = argparse.ArgumentParser(description="ADB sampler for battery/power related telemetry with disconnect handling")
    parser.add_argument("--adb", default=None, help="Path to adb (default: auto-detect)")
    parser.add_argument("--serial", default=None, help="Device serial (optional)")
    parser.add_argument("--interval", type=float, default=2.0, help="Sampling interval seconds")
    parser.add_argument("--duration", type=float, default=60.0, help="Total duration seconds")
    parser.add_argument("--out", type=Path, default=None, help="Output CSV path (default: artifacts/runs/<run_id>.csv)")
    parser.add_argument("--scenario", default="S0", help="Scenario label")
    parser.add_argument("--auto-reset-battery", action="store_true", help="Auto reset battery service if UPDATES STOPPED")
    parser.add_argument("--policies", default="0,4,7", help="Comma-separated cpufreq policies to sample")
    parser.add_argument("--thermal", action="store_true", help="Also sample dumpsys thermalservice")
    parser.add_argument("--display", action="store_true", help="Also sample dumpsys power display state")
    parser.add_argument(
        "--batteryproperties",
        action="store_true",
        help="Also sample dumpsys batteryproperties (current_now/current_average/energy_counter if available)",
    )
    parser.add_argument(
        "--thermal-names",
        default="BATTERY,SKIN,SOC,CPU,GPU,NPU,TPU,POWER_AMPLIFIER",
        help="Comma-separated thermal sensor names to extract when --thermal is enabled",
    )
    parser.add_argument(
        "--log-every",
        type=float,
        default=60.0,
        help="Print a progress line every N seconds (0 disables)",
    )
    parser.add_argument(
        "--policy-knobs",
        action="store_true",
        help=(
            "Also sample scheduler/power policy knobs (cpufreq min/max/governor, cpu online, cpuset, uclamp) "
            "to help detect boost/throttle windows. Best-effort and device-dependent."
        ),
    )
    parser.add_argument(
        "--policy-knobs-period-s",
        type=float,
        default=0.0,
        help=(
            "If --policy-knobs is enabled, sample knobs at most once per this period (seconds) and reuse the last values "
            "for intermediate rows. Use this to reduce overhead (default: 0 = every sample)."
        ),
    )
    parser.add_argument(
        "--policy-services",
        default="",
        help=(
            "Comma-separated dumpsys services to sample for explicit policy state (e.g. "
            "SchedBoostService,miui.whetstone.power,performance_hint,power). "
            "Use with --policy-services-period-s to reduce overhead."
        ),
    )
    parser.add_argument(
        "--policy-services-period-s",
        type=float,
        default=30.0,
        help=(
            "If --policy-services is set, sample these services at most once per this period (seconds) and reuse the last values."
        ),
    )
    parser.add_argument(
        "--policy-services-timeout-s",
        type=float,
        default=8.0,
        help="Per-service dumpsys timeout (seconds) when --policy-services is enabled.",
    )
    args = parser.parse_args()

    adb = _resolve_adb(args.adb)

    if not args.serial:
        picked = _pick_default_serial(adb, timeout_s=8.0)
        if picked is None:
            raise SystemExit(
                "No ADB device found. If using wireless debugging, pair/connect first, then pass --serial <serial>."
            )
        args.serial = picked

    run_id = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    out_path: Path = args.out if args.out is not None else Path("artifacts") / "runs" / f"{run_id}_{args.scenario}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    policies = []
    for p in args.policies.split(','):
        p = p.strip()
        if not p:
            continue
        try:
            policies.append(int(p))
        except Exception:
            raise SystemExit(f"Invalid policy id: {p}")

    state = TimeInStateState(last={})

    want_thermal_names: set[str] = set()
    if args.thermal:
        want_thermal_names = {x.strip().upper() for x in str(args.thermal_names).split(",") if x.strip()}

    # First ensure ready (handles temporary disconnects)
    try:
        _ensure_device_ready(adb, args.serial, timeout_s=15.0)
    except Exception as e:
        raise SystemExit(f"ADB device not ready: {e}")

    # Determine dynamic columns based on first time_in_state snapshot
    current_tis: dict[int, dict[int, int]] = {}
    for policy in policies:
        t = _read_time_in_state(adb, args.serial, policy, timeout_s=5.0)
        if t:
            current_tis[policy] = t
    delta_cols = sorted(_delta_time_in_state(state, current_tis).keys())

    fixed_cols = [
        "run_id",
        "seq",
        "ts_pc",
        "scenario",
        "note",
        "battery_level",
        "battery_scale",
        "battery_status",
        "battery_plugged",
        "battery_ac_powered",
        "battery_usb_powered",
        "battery_wireless_powered",
        "battery_voltage_mv",
        "battery_temp_deciC",
        "charge_counter_uAh",
        "brightness",
        "battery_updates_stopped",
        "adb_error",
    ]

    if args.policy_knobs:
        fixed_cols.append("cpu_online")
        fixed_cols.extend(
            [
                "cpuset_top_app",
                "cpuset_foreground",
                "cpuset_background",
                "cpuset_system_background",
                "uclamp_top_app_max",
                "uclamp_top_app_min",
                "uclamp_foreground_max",
                "uclamp_foreground_min",
            ]
        )
        for p in policies:
            fixed_cols.extend(
                [
                    f"cpu_p{p}_scaling_min_freq_khz",
                    f"cpu_p{p}_scaling_max_freq_khz",
                    f"cpu_p{p}_scaling_governor",
                ]
            )

    policy_services: list[str] = []
    if str(args.policy_services).strip():
        policy_services = [s.strip() for s in str(args.policy_services).split(",") if s.strip()]
        # Add stable columns up-front.
        for svc in policy_services:
            fixed_cols.extend(_policy_service_columns(svc))

    if args.batteryproperties:
        fixed_cols.extend(
            [
                "batteryproperties_current_now_uA",
                "batteryproperties_current_average_uA",
                "batteryproperties_energy_counter",
                "batteryproperties_charge_counter_uAh",
            ]
        )

    if args.display:
        fixed_cols.append("display_state")

    if args.thermal:
        fixed_cols.append("thermal_status")
        for name in sorted(want_thermal_names):
            fixed_cols.append(f"thermal_{name.lower()}_C")
    cols = fixed_cols + delta_cols

    t_end = time.time() + float(args.duration)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()

        seq = 0
        last_log_t = 0.0
        last_knobs: dict[str, str] = {}
        last_knobs_t = 0.0
        last_policy_services: dict[str, object] = {}
        last_policy_services_t = 0.0
        if args.log_every and args.log_every > 0:
            print(f"Sampling -> {out_path} (interval={args.interval}s, duration={args.duration}s)")
        while time.time() < t_end:
            ts = _iso_now()
            row: dict[str, object] = {c: "" for c in cols}
            row["run_id"] = run_id
            row["seq"] = seq
            row["ts_pc"] = ts
            row["scenario"] = args.scenario
            row["note"] = ""

            try:
                _ensure_device_ready(adb, args.serial, timeout_s=10.0)
                batt = _read_battery(adb, args.serial, timeout_s=8.0, auto_reset=args.auto_reset_battery)
                row["battery_level"] = batt.level
                row["battery_scale"] = batt.scale
                row["battery_status"] = batt.status
                row["battery_plugged"] = batt.plugged
                row["battery_ac_powered"] = batt.ac_powered
                row["battery_usb_powered"] = batt.usb_powered
                row["battery_wireless_powered"] = batt.wireless_powered
                row["battery_voltage_mv"] = batt.voltage_mv
                row["battery_temp_deciC"] = batt.temp_deci_c
                row["charge_counter_uAh"] = batt.charge_counter_uah
                row["battery_updates_stopped"] = int(batt.raw_updates_stopped)

                row["brightness"] = _read_brightness(adb, args.serial, timeout_s=4.0)

                if args.batteryproperties:
                    bp = _read_batteryproperties(adb, args.serial, timeout_s=6.0)
                    row["batteryproperties_current_now_uA"] = bp.current_now_uA
                    row["batteryproperties_current_average_uA"] = bp.current_average_uA
                    row["batteryproperties_energy_counter"] = bp.energy_counter
                    row["batteryproperties_charge_counter_uAh"] = bp.charge_counter_uAh

                if args.display:
                    row["display_state"] = _read_display_state(adb, args.serial, timeout_s=8.0) or ""

                if args.thermal:
                    therm = _read_thermalservice(adb, args.serial, timeout_s=8.0, want_names=want_thermal_names)
                    for k, v in therm.items():
                        if k in row:
                            row[k] = v

                if args.policy_knobs:
                    # Throttle knob sampling to reduce ADB overhead.
                    now_t = time.time()
                    period = float(args.policy_knobs_period_s or 0.0)
                    do_read = (period <= 0.0) or (last_knobs_t <= 0.0) or ((now_t - last_knobs_t) >= period)
                    if do_read:
                        last_knobs = _read_policy_knobs(adb, args.serial, policies=policies, timeout_s=6.0)
                        last_knobs_t = now_t
                    knobs = last_knobs
                    # Copy known keys. Missing keys remain empty.
                    row["cpu_online"] = knobs.get("cpu_online", "")
                    for k in [
                        "cpuset_top_app",
                        "cpuset_foreground",
                        "cpuset_background",
                        "cpuset_system_background",
                        "uclamp_top_app_max",
                        "uclamp_top_app_min",
                        "uclamp_foreground_max",
                        "uclamp_foreground_min",
                    ]:
                        if k in row:
                            row[k] = knobs.get(k, "")
                    for p in policies:
                        mn = knobs.get(f"cpu_p{p}_scaling_min_freq", "")
                        mx = knobs.get(f"cpu_p{p}_scaling_max_freq", "")
                        gov = knobs.get(f"cpu_p{p}_scaling_governor", "")
                        row[f"cpu_p{p}_scaling_min_freq_khz"] = mn
                        row[f"cpu_p{p}_scaling_max_freq_khz"] = mx
                        row[f"cpu_p{p}_scaling_governor"] = gov

                if policy_services:
                    now_t = time.time()
                    period = float(args.policy_services_period_s or 0.0)
                    do_read = (period <= 0.0) or (last_policy_services_t <= 0.0) or ((now_t - last_policy_services_t) >= period)
                    if do_read:
                        base = ["-s", args.serial] if args.serial else []
                        merged: dict[str, object] = {}
                        for svc in policy_services:
                            key = _sanitize_key(svc)
                            prefix = f"policy_{key}_"
                            try:
                                rc, out, err = _run(
                                    adb,
                                    [*base, "shell", "dumpsys", svc],
                                    timeout_s=float(args.policy_services_timeout_s),
                                )
                                text = out + ("\n" + err if err else "")
                                merged[prefix + "rc"] = rc
                                merged[prefix + "sha1"] = _sha1_text(text) if text else ""
                                merged.update(_parse_dumpsys_policy_service(svc, text))
                            except Exception:
                                merged[prefix + "rc"] = ""
                                merged[prefix + "sha1"] = ""
                        last_policy_services = merged
                        last_policy_services_t = now_t
                    for k, v in last_policy_services.items():
                        if k in row:
                            row[k] = v

                current_tis = {}
                for policy in policies:
                    t = _read_time_in_state(adb, args.serial, policy, timeout_s=5.0)
                    if t:
                        current_tis[policy] = t
                deltas = _delta_time_in_state(state, current_tis)

                # If a new frequency appears later, we ignore it in v0 (keeps CSV stable).
                for k, v in deltas.items():
                    if k in row:
                        row[k] = v

                row["adb_error"] = ""

            except TimeoutError as e:
                row["adb_error"] = f"timeout:{e}"
            except Exception as e:
                # 包含“假断电/断连/adb 卡住”等
                row["adb_error"] = f"error:{type(e).__name__}:{e}"

            writer.writerow(row)
            f.flush()
            seq += 1

            if args.log_every and args.log_every > 0:
                now_t = time.time()
                if now_t - last_log_t >= float(args.log_every):
                    last_log_t = now_t
                    v = row.get("battery_voltage_mv", "")
                    lvl = row.get("battery_level", "")
                    err = row.get("adb_error", "")
                    print(f"[sample] seq={seq} ts={ts} level={lvl} voltage_mv={v} adb_error={err}")
            time.sleep(float(args.interval))

    print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
