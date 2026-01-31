from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class BrightnessState:
    brightness: int | None
    mode: int | None  # 0 manual, 1 auto
    timeout_ms: int | None


def _run(adb: str, serial: str | None, args: list[str], timeout_s: float = 8.0) -> tuple[int, str, str]:
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["shell", *args]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_s)
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    return proc.returncode, out, err


def _shell_ok(adb: str, serial: str | None, args: list[str], timeout_s: float = 8.0) -> str:
    rc, out, err = _run(adb, serial, args, timeout_s=timeout_s)
    if rc != 0:
        raise RuntimeError((err or out).strip() or f"adb shell failed: {' '.join(args)}")
    return out


def _get_int_setting(adb: str, serial: str | None, namespace: str, key: str) -> int | None:
    rc, out, _ = _run(adb, serial, ["settings", "get", namespace, key])
    if rc != 0:
        return None
    s = out.strip()
    if not s or s.lower() == "null":
        return None
    try:
        return int(float(s))
    except Exception:
        return None


def _ensure_write_settings(adb: str, serial: str | None) -> None:
    """Enable WRITE_SETTINGS for com.android.shell via AppOps (works on many OEM builds).

    This is required for `settings put system ...` to succeed on some devices.
    """
    _shell_ok(adb, serial, ["appops", "set", "com.android.shell", "WRITE_SETTINGS", "allow"], timeout_s=8.0)


def _set_system_setting(adb: str, serial: str | None, key: str, value: int) -> None:
    _shell_ok(adb, serial, ["settings", "put", "system", key, str(int(value))], timeout_s=8.0)


def read_state(adb: str, serial: str | None) -> BrightnessState:
    return BrightnessState(
        brightness=_get_int_setting(adb, serial, "system", "screen_brightness"),
        mode=_get_int_setting(adb, serial, "system", "screen_brightness_mode"),
        timeout_ms=_get_int_setting(adb, serial, "system", "screen_off_timeout"),
    )


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "Run one S2 brightness step using the *current* device brightness value. "
            "This is designed for devices where adb cannot WRITE_SETTINGS or inject input events.\n\n"
            "Workflow: set brightness manually on the phone -> run this script -> it auto-names scenario as S2_b<value>."
        )
    )
    p.add_argument("--adb", default="C:/Users/30483/AppData/Local/Android/Sdk/platform-tools/adb.exe")
    p.add_argument("--serial", default=None)
    p.add_argument("--scenario-prefix", default="S2")
    p.add_argument("--duration", type=float, default=540.0, help="Seconds. Keep <= screen timeout (default 9 min).")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--thermal", action="store_true")
    p.add_argument("--display", action="store_true", help="Also sample display state via dumpsys power")
    p.add_argument(
        "--batteryproperties",
        action="store_true",
        help="Also sample dumpsys batteryproperties (current_now/current_average if available)",
    )
    p.add_argument("--auto-reset-battery", action="store_true")
    p.add_argument(
        "--batterystats-proto",
        action="store_true",
        help="Capture `dumpsys batterystats --proto` before/after and parse coulomb-counter discharge totals (schema-min).",
    )
    p.add_argument(
        "--batterystats-proto-reset",
        action="store_true",
        help="Run `dumpsys batterystats --reset` before capturing START proto.",
    )
    p.add_argument(
        "--perfetto-android-power",
        action="store_true",
        help=(
            "Record Perfetto android.power battery counters during the run and parse them into "
            "perfetto_android_power_summary.csv/json + timeseries.csv in the report dir."
        ),
    )
    p.add_argument(
        "--perfetto-battery-poll-ms",
        type=int,
        default=250,
        help="Perfetto android.power battery polling period (ms).",
    )
    p.add_argument("--log-every", type=float, default=30.0)
    p.add_argument(
        "--set-brightness",
        type=int,
        default=None,
        help="If provided, try to set manual brightness (0-255) via adb before running.",
    )
    p.add_argument(
        "--set-timeout-ms",
        type=int,
        default=None,
        help="If provided, try to set screen_off_timeout (ms) via adb before running.",
    )
    p.add_argument(
        "--enable-write-settings",
        action="store_true",
        help="Try `appops set com.android.shell WRITE_SETTINGS allow` so adb can change system settings.",
    )
    p.add_argument(
        "--allow-auto-brightness",
        action="store_true",
        help="Allow running even if screen_brightness_mode==1 (auto). Not recommended for S2.",
    )
    args = p.parse_args()

    if args.set_brightness is not None and not (0 <= args.set_brightness <= 255):
        raise SystemExit("--set-brightness must be in [0, 255]")

    if args.set_timeout_ms is not None and args.set_timeout_ms <= 0:
        raise SystemExit("--set-timeout-ms must be positive")

    # Optional: enable WRITE_SETTINGS via AppOps and set parameters programmatically.
    if args.enable_write_settings and (args.set_brightness is not None or args.set_timeout_ms is not None):
        try:
            _ensure_write_settings(args.adb, args.serial)
        except Exception as e:
            raise SystemExit(
                "Failed to enable WRITE_SETTINGS via appops. "
                "You can still do S2 by setting brightness manually on the phone and running without --set-brightness. "
                f"Details: {e}"
            )

    if args.set_brightness is not None:
        try:
            # enforce manual mode for S2
            _set_system_setting(args.adb, args.serial, "screen_brightness_mode", 0)
            _set_system_setting(args.adb, args.serial, "screen_brightness", int(args.set_brightness))
        except Exception as e:
            raise SystemExit(
                "Failed to set brightness via adb. "
                "If your device restricts WRITE_SETTINGS, try adding --enable-write-settings, or set it manually. "
                f"Details: {e}"
            )

    if args.set_timeout_ms is not None:
        try:
            _set_system_setting(args.adb, args.serial, "screen_off_timeout", int(args.set_timeout_ms))
        except Exception as e:
            raise SystemExit(
                "Failed to set screen_off_timeout via adb. "
                "Try setting it in Settings UI, or run each S2 step shorter than the device timeout. "
                f"Details: {e}"
            )

    st = read_state(args.adb, args.serial)
    print(f"screen_brightness={st.brightness} (0-255)")
    print(f"screen_brightness_mode={st.mode} (0 manual, 1 auto)")
    print(f"screen_off_timeout_ms={st.timeout_ms}")

    if st.brightness is None:
        raise SystemExit("Could not read screen_brightness via adb. Is the device connected/authorized?")

    if st.mode == 1 and not args.allow_auto_brightness:
        raise SystemExit(
            "Auto-brightness is ON (screen_brightness_mode=1). For a rigorous S2, turn it OFF (manual) and retry. "
            "If you insist, pass --allow-auto-brightness."
        )

    scenario = f"{args.scenario_prefix}_b{st.brightness}"

    cmd = [
        sys.executable,
        "scripts/pipeline_run.py",
        "--adb",
        args.adb,
        "--scenario",
        scenario,
        "--duration",
        str(args.duration),
        "--interval",
        str(args.interval),
        "--log-every",
        str(args.log_every),
    ]
    if args.serial:
        cmd += ["--serial", args.serial]
    if args.thermal:
        cmd += ["--thermal"]
    if args.display:
        cmd += ["--display"]
    if args.batteryproperties:
        cmd += ["--batteryproperties"]
    if args.auto_reset_battery:
        cmd += ["--auto-reset-battery"]
    if args.batterystats_proto:
        cmd += ["--batterystats-proto"]
    if args.batterystats_proto_reset:
        cmd += ["--batterystats-proto-reset"]
    if args.perfetto_android_power:
        cmd += ["--perfetto-android-power", "--perfetto-battery-poll-ms", str(args.perfetto_battery_poll_ms)]

    print("Running:", " ".join(cmd))
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    raise SystemExit(main())
