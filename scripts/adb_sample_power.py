from __future__ import annotations

import argparse
import csv
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
    raw_updates_stopped: bool


_BATT_KV = {
    "level": re.compile(r"^\s*level:\s*(\d+)\s*$", re.MULTILINE),
    "scale": re.compile(r"^\s*scale:\s*(\d+)\s*$", re.MULTILINE),
    "voltage": re.compile(r"^\s*voltage:\s*(\d+)\s*$", re.MULTILINE),
    "temperature": re.compile(r"^\s*temperature:\s*(\d+)\s*$", re.MULTILINE),
    "charge_counter": re.compile(r"^\s*Charge counter:\s*(\d+)\s*$", re.MULTILINE),
}


def _parse_int(regex: re.Pattern[str], text: str) -> int | None:
    m = regex.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
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
        raw_updates_stopped=updates_stopped,
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


def _read_display_state(adb: str, serial: str | None, timeout_s: float) -> str | None:
    """Best-effort display state from `dumpsys power`.

    Returns one of ON/OFF/DOZE/UNKNOWN-ish strings, or None if unavailable.
    """
    base = ["-s", serial] if serial else []
    rc, out, _ = _run(adb, [*base, "shell", "dumpsys", "power"], timeout_s=timeout_s)
    if rc != 0:
        return None
    m = _RE_DISPLAY_POWER_STATE.search(out)
    if not m:
        return None
    return m.group(1)


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
        "battery_voltage_mv",
        "battery_temp_deciC",
        "charge_counter_uAh",
        "brightness",
        "battery_updates_stopped",
        "adb_error",
    ]

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
                row["battery_voltage_mv"] = batt.voltage_mv
                row["battery_temp_deciC"] = batt.temp_deci_c
                row["charge_counter_uAh"] = batt.charge_counter_uah
                row["battery_updates_stopped"] = int(batt.raw_updates_stopped)

                row["brightness"] = _read_brightness(adb, args.serial, timeout_s=4.0)

                if args.display:
                    row["display_state"] = _read_display_state(adb, args.serial, timeout_s=8.0) or ""

                if args.thermal:
                    therm = _read_thermalservice(adb, args.serial, timeout_s=8.0, want_names=want_thermal_names)
                    for k, v in therm.items():
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
