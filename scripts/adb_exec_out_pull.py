from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull a remote file via `adb exec-out cat` (binary-safe).")
    parser.add_argument("--adb", required=True, help="Path to adb executable")
    parser.add_argument("remote", help="Remote path on device")
    parser.add_argument("local", type=Path, help="Local output path")
    args = parser.parse_args()

    local: Path = args.local
    local.parent.mkdir(parents=True, exist_ok=True)

    cmd = [args.adb, "exec-out", "cat", args.remote]
    with local.open("wb") as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        raise SystemExit(f"adb failed (exit={proc.returncode}): {stderr.strip()}")

    if local.stat().st_size == 0:
        raise SystemExit(f"Pulled file is empty: {local} (remote: {args.remote})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
