from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from mp_power.adb import adb_shell
from mp_power.adb import run_adb


DEFAULT_PID_FILE = "/data/local/tmp/mp_power_cpu_load.pids"


def _push_and_run_script(
    adb: str,
    serial: str | None,
    script_text: str,
    *,
    remote_path: str,
    push_timeout_s: float,
    run_timeout_s: float,
) -> tuple[int, str, str]:
    # Pushing a script avoids extremely long `sh -c` arguments, which can hang on some setups.
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".sh", encoding="utf-8", newline="\n") as f:
        local = f.name
        f.write(script_text)
        if not script_text.endswith("\n"):
            f.write("\n")

    try:
        base = ["-s", serial] if serial else []
        rc, out, err = run_adb(adb, [*base, "push", local, remote_path], timeout_s=push_timeout_s)
        if rc != 0:
            return rc, out, err

        return adb_shell(
            adb,
            serial,
            [
                "sh",
                "-c",
                f"chmod 700 {remote_path} >/dev/null 2>&1; sh {remote_path}; RC=$?; rm -f {remote_path} >/dev/null 2>&1; exit $RC",
            ],
            timeout_s=run_timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        return 124, "", f"TimeoutExpired: {e}"
    finally:
        try:
            Path(local).unlink(missing_ok=True)
        except Exception:
            pass


def cpu_load_start(
    adb: str,
    serial: str | None,
    threads: int,
    *,
    pid_file: str = DEFAULT_PID_FILE,
) -> None:
    """Start best-effort CPU busy-loop workers on device and record their PIDs.

    No root; no extra binaries; uses `/data/local/tmp`.
    """

    threads = int(threads)
    if threads <= 0:
        return

    if serial and "_adb-tls-connect._tcp" in serial:
        print(
            "WARN: CPU load over wireless debugging mDNS serial may be unstable. "
            "If this times out, prefer USB or connect via ip:port so adb uses a stable serial."
        )

    def _run_start_script(work_cmd: str) -> tuple[int, str, str]:
        script = "\n".join(
            [
                f"pid_file=\"{pid_file}\"",
                "set +e",
                "if [ -f \"$pid_file\" ]; then",
                "  PIDS_OLD=\"$(cat \"$pid_file\")\"",
                "  for p in $PIDS_OLD; do kill $p >/dev/null 2>&1; done",
                "  sleep 0.1",
                "  for p in $PIDS_OLD; do kill -9 $p >/dev/null 2>&1; done",
                "  rm -f \"$pid_file\"",
                "fi",
                "echo 0 > \"${pid_file}.tmp\" 2>/dev/null || exit 41",
                "rm -f \"${pid_file}.tmp\" >/dev/null 2>&1",
                "PIDS=\"\"",
                "i=0",
                "HAS_NICE=0; command -v nice >/dev/null 2>&1 && HAS_NICE=1",
                f"threads={threads}",
                "while [ $i -lt $threads ]; do",
                "  TAG=mp_power_cpu_load_worker_$i",
                "  if [ $HAS_NICE -eq 1 ]; then",
                "    (nice -n 19 sh -c \"" + work_cmd.replace("\"", "\\\"") + "\" $TAG) >/dev/null 2>&1 &",
                "  else",
                "    (sh -c \"" + work_cmd.replace("\"", "\\\"") + "\" $TAG) >/dev/null 2>&1 &",
                "  fi",
                "  PIDS=\"$PIDS $!\"",
                "  i=$((i+1))",
                "  sleep 0.05",
                "done",
                "for p in $PIDS; do kill -0 $p >/dev/null 2>&1 || exit 42; done",
                "echo $PIDS > \"$pid_file\" || exit 43",
                "echo started:$PIDS",
            ]
        )
        return _push_and_run_script(
            adb,
            serial,
            script,
            remote_path="/data/local/tmp/mp_power_cpu_load_start.sh",
            push_timeout_s=20.0,
            run_timeout_s=60.0,
        )

    yielding_loop = (
        "if command -v usleep >/dev/null 2>&1; then "
        "  while true; do :; usleep 20000; done; "
        "else "
        "  while true; do :; sleep 0.02; done; "
        "fi"
    )

    rc, out, err = _run_start_script(yielding_loop)
    if rc == 0:
        print(f"CPU load: started threads={threads} (pid_file={pid_file})")
        return

    rc2, out2, err2 = _run_start_script("yes >/dev/null")
    if rc2 == 0:
        print(f"CPU load: started threads={threads} (pid_file={pid_file})")
        return

    details = (
        f"cpu load start failed (threads={threads}, pid_file={pid_file})\n"
        f"A(rc={rc}) out={out.strip()!r} err={err.strip()!r}\n"
        f"B(rc={rc2}) out={out2.strip()!r} err={err2.strip()!r}"
    )
    raise RuntimeError(details)


def cpu_load_stop(
    adb: str,
    serial: str | None,
    *,
    pid_file: str = DEFAULT_PID_FILE,
    timeout_s: float = 30.0,
) -> None:
    """Stop workers previously started by cpu_load_start (best-effort).

    Uses a background subshell so adb can return quickly (important for wireless debugging stability).
    """

    script = "\n".join(
        [
            f"pid_file=\"{pid_file}\"",
            "set +e",
            "PIDS=\"\"",
            "if [ -f \"$pid_file\" ]; then",
            "  PIDS=\"$(cat \"$pid_file\")\"",
            "  rm -f \"$pid_file\" >/dev/null 2>&1",
            "fi",
            "HAS_PKILL=0; command -v pkill >/dev/null 2>&1 && HAS_PKILL=1",
            "( ",
            "  if [ -n \"$PIDS\" ]; then",
            "    for p in $PIDS; do kill $p >/dev/null 2>&1; done",
            "    sleep 0.2",
            "    for p in $PIDS; do kill -9 $p >/dev/null 2>&1; done",
            "  elif [ $HAS_PKILL -eq 1 ]; then",
            "    pkill -f mp_power_cpu_load_worker_ >/dev/null 2>&1",
            "    sleep 0.2",
            "    pkill -9 -f mp_power_cpu_load_worker_ >/dev/null 2>&1",
            "  fi",
            ") >/dev/null 2>&1 < /dev/null &",
            "echo stop-queued",
        ]
    )

    try:
        rc, out, err = adb_shell(adb, serial, ["sh", "-c", script], timeout_s=timeout_s)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"cpu load stop TimeoutExpired: {e}")

    if rc != 0:
        raise RuntimeError((err or out).strip() or "failed to stop cpu load")

    if "stop-queued" in (out or ""):
        print("CPU load: stop queued")
    else:
        print("CPU load: stop requested")
