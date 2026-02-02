from __future__ import annotations

import argparse
import time

from _bootstrap import ensure_repo_root_on_sys_path

ensure_repo_root_on_sys_path()

from mp_power.adb import adb_shell
from mp_power.adb import ensure_device_ready
from mp_power.adb import pick_default_serial
from mp_power.adb import resolve_adb
from mp_power.cpu_load import DEFAULT_PID_FILE
from mp_power.cpu_load import cpu_load_start
from mp_power.cpu_load import cpu_load_stop


def _read_pids(adb: str, serial: str | None, pid_file: str) -> list[int]:
    rc, out, _ = adb_shell(adb, serial, ["sh", "-c", f"if [ -f {pid_file} ]; then cat {pid_file}; fi"], timeout_s=8.0)
    if rc != 0:
        return []
    pids: list[int] = []
    for tok in (out or "").strip().split():
        try:
            pids.append(int(tok))
        except Exception:
            continue
    return pids


def _all_dead(adb: str, serial: str | None, pids: list[int]) -> bool:
    if not pids:
        return True
    # kill -0 checks existence without killing.
    cmd = " && ".join([f"kill -0 {p} >/dev/null 2>&1 || exit 1" for p in pids])
    rc, _, _ = adb_shell(adb, serial, ["sh", "-c", cmd], timeout_s=8.0)
    return rc != 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke-test on-device CPU load start/stop (no sampling).")
    ap.add_argument("--adb", default=None, help="adb path (optional)")
    ap.add_argument("--serial", default=None, help="Device serial (optional)")
    ap.add_argument("--threads", type=int, default=4, help="CPU worker threads to start")
    ap.add_argument("--hold-s", type=float, default=10.0, help="Seconds to hold load between start/stop")
    ap.add_argument("--iters", type=int, default=1, help="Repeat start/stop cycles")
    ap.add_argument("--between-s", type=float, default=2.0, help="Sleep between cycles")
    ap.add_argument("--pid-file", default=DEFAULT_PID_FILE, help="Remote pid file path")
    ap.add_argument(
        "--verify-stop-timeout-s",
        type=float,
        default=8.0,
        help="Max seconds to wait for processes to disappear after stop (async stop may take a moment)",
    )
    args = ap.parse_args()

    adb = resolve_adb(args.adb)
    serial = args.serial or pick_default_serial(adb, timeout_s=8.0)
    if not serial:
        raise SystemExit("No adb devices found. Provide --serial or connect a device.")

    ensure_device_ready(adb, serial, timeout_s=15.0)

    threads = int(args.threads)
    if threads <= 0:
        raise SystemExit("--threads must be > 0")

    for i in range(int(args.iters)):
        print(f"[cpu_load_smoke] cycle {i+1}/{int(args.iters)} serial={serial} threads={threads}")

        cpu_load_start(adb, serial, threads, pid_file=str(args.pid_file))
        pids = _read_pids(adb, serial, str(args.pid_file))
        print(f"[cpu_load_smoke] started pids={pids}")

        time.sleep(float(args.hold_s))

        cpu_load_stop(adb, serial, pid_file=str(args.pid_file), timeout_s=30.0)

        # Stop is queued in background; poll briefly for disappearance.
        t0 = time.time()
        while time.time() - t0 < float(args.verify_stop_timeout_s):
            if _all_dead(adb, serial, pids):
                break
            time.sleep(0.5)

        if not _all_dead(adb, serial, pids):
            print("[cpu_load_smoke] WARN: workers still alive after timeout (wireless debugging may be laggy)")

        if i + 1 < int(args.iters):
            time.sleep(float(args.between_s))

    print("[cpu_load_smoke] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
