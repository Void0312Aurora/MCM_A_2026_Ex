from __future__ import annotations

import argparse
from pathlib import Path

from _bootstrap import ensure_repo_root_on_sys_path

ensure_repo_root_on_sys_path()

from mp_power.adb import adb_exec_out
from mp_power.adb import resolve_adb


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull a remote file via `adb exec-out cat` (binary-safe).")
    parser.add_argument("--adb", default=None, help="Path to adb executable (optional; auto-detect by default)")
    parser.add_argument("--serial", default=None, help="Device serial (optional)")
    parser.add_argument("remote", help="Remote path on device")
    parser.add_argument("local", type=Path, help="Local output path")
    args = parser.parse_args()

    adb = resolve_adb(args.adb)

    local: Path = args.local
    local.parent.mkdir(parents=True, exist_ok=True)

    rc, blob, err = adb_exec_out(adb, args.serial, ["cat", args.remote], timeout_s=30.0)
    if rc != 0:
        raise SystemExit(f"adb failed (exit={rc}): {err.strip()}")

    local.write_bytes(blob)

    if local.stat().st_size == 0:
        raise SystemExit(f"Pulled file is empty: {local} (remote: {args.remote})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
