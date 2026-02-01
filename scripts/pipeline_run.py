from __future__ import annotations

import argparse
import re
import subprocess
from datetime import datetime
from pathlib import Path

from _bootstrap import ensure_repo_root_on_sys_path

ensure_repo_root_on_sys_path()

from mp_power.adb import adb_exec_out
from mp_power.adb import adb_shell
from mp_power.adb import pick_default_serial
from mp_power.adb import resolve_adb
from mp_power.adb import shell_ok
from mp_power.pipeline_ops import enrich_run_with_cpu_energy
from mp_power.pipeline_ops import parse_perfetto_android_power_counters
from mp_power.pipeline_ops import parse_perfetto_policy_markers
from mp_power.pipeline_ops import report_run
from mp_power.pipeline_ops import write_batterystats_min_summary
from mp_power.pipeline_ops import parse_power_profile_xmltree
from mp_power.pipeline_ops import write_power_profile_outputs


def _run(cmd: list[str], timeout_s: float | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def _capture_wrote_path(output: str) -> Path | None:
    # Accept both Windows backslashes and posix slashes
    m = re.search(r"^Wrote:\s*(.+?)\s*$", output, re.MULTILINE)
    if not m:
        return None
    return Path(m.group(1).strip())


_RE_BS_GLOBAL = re.compile(r"^\s*Global\s*$", re.MULTILINE)
_RE_BS_LINE = re.compile(
    r"^\s*(?P<name>[a-zA-Z0-9_]+):\s*(?P<mah>[0-9.]+)\b(?P<rest>.*)$",
    re.MULTILINE,
)


def _parse_batterystats_usage_global(text: str) -> dict[str, float]:
    """Parse `dumpsys batterystats --usage` and return Global component mAh.

    Example lines:
      Global
        screen: 941 apps: 941 duration: ...
        wifi: 89.2 apps: 82.3 duration: ...
    """
    # Heuristic: start at the "Global" section and stop when an unindented "UID" line appears.
    m = _RE_BS_GLOBAL.search(text)
    if not m:
        return {}
    section = text[m.end() :]
    stop = re.search(r"^\s*UID\b", section, re.MULTILINE)
    if stop:
        section = section[: stop.start()]

    out: dict[str, float] = {}
    for lm in _RE_BS_LINE.finditer(section):
        name = lm.group("name")
        try:
            out[name] = float(lm.group("mah"))
        except Exception:
            continue
    return out


def _ensure_write_settings(adb: str, serial: str | None) -> None:
    # Best-effort: some OEM builds require this for `settings put system ...`.
    shell_ok(adb, serial, ["appops", "set", "com.android.shell", "WRITE_SETTINGS", "allow"], timeout_s=8.0)


def _set_system_setting(adb: str, serial: str | None, key: str, value: int) -> None:
    shell_ok(adb, serial, ["settings", "put", "system", key, str(int(value))], timeout_s=8.0)


def _get_system_setting(adb: str, serial: str | None, key: str) -> str | None:
    rc, out, _ = adb_shell(adb, serial, ["settings", "get", "system", key], timeout_s=8.0)
    if rc != 0:
        return None
    s = (out or "").strip()
    if not s or s.lower() == "null":
        return None
    return s


def _cpu_load_start(adb: str, serial: str | None, threads: int) -> None:
    """Start best-effort CPU busy-loop workers on device and record their PIDs.

    This is intentionally simple and dependency-free (no root, no extra binaries).
    """

    threads = int(threads)
    if threads <= 0:
        return

    # Store PIDs so we can stop reliably even if process names differ across devices.
    pid_file = "/data/local/tmp/mp_power_cpu_load.pids"
    # Start gradually and (if possible) with lower priority so adb shell itself still gets CPU time.
    # Give each worker a distinctive $0 argv marker so we can pkill it even if the pid file is missing.
    script = (
        # Pre-clean leftovers from previous aborted runs.
        "HAS_PKILL=0; command -v pkill >/dev/null 2>&1 && HAS_PKILL=1; "
        "if [ -f " + pid_file + " ]; then "
        "  for p in $(cat " + pid_file + "); do kill $p >/dev/null 2>&1; done; "
        "  sleep 0.1; "
        "  for p in $(cat " + pid_file + "); do kill -9 $p >/dev/null 2>&1; done; "
        "  rm -f " + pid_file + "; "
        "fi; "
        "if [ $HAS_PKILL -eq 1 ]; then pkill -f mp_power_cpu_load_worker >/dev/null 2>&1 || true; fi; "
        # Start new workers.
        "PIDS=; i=0; "
        "HAS_NICE=0; command -v nice >/dev/null 2>&1 && HAS_NICE=1; "
        f"while [ $i -lt {threads} ]; do "
        "  TAG=mp_power_cpu_load_worker_$i; "
        "  if [ $HAS_NICE -eq 1 ]; then "
        "    (nice -n 10 sh -c 'while true; do :; done' $TAG) >/dev/null 2>&1 & "
        "  else "
        "    (sh -c 'while true; do :; done' $TAG) >/dev/null 2>&1 & "
        "  fi; "
        "  PIDS=\"$PIDS $!\"; "
        "  i=$((i+1)); "
        "  sleep 0.05; "
        "done; "
        f"echo $PIDS > {pid_file}; "
        "echo started:$PIDS"
    )

    rc, out, err = adb_shell(adb, serial, ["sh", "-c", script], timeout_s=60.0)
    if rc != 0:
        raise RuntimeError((err or out).strip() or "failed to start cpu load")
    print(f"CPU load: started threads={threads} (pid_file={pid_file})")


def _cpu_load_stop(adb: str, serial: str | None) -> None:
    """Stop CPU load workers previously started by _cpu_load_start (best-effort)."""

    pid_file = "/data/local/tmp/mp_power_cpu_load.pids"
    script = (
        "HAS_PKILL=0; command -v pkill >/dev/null 2>&1 && HAS_PKILL=1; "
        f"if [ -f {pid_file} ]; then "
        f"  PIDS=\"$(cat {pid_file})\"; "
        "  for p in $PIDS; do kill $p >/dev/null 2>&1; done; "
        "  sleep 0.1; "
        "  for p in $PIDS; do kill -9 $p >/dev/null 2>&1; done; "
        f"  rm -f {pid_file}; "
        "fi; "
        # Fallback cleanup if pid file is missing/stale.
        "if [ $HAS_PKILL -eq 1 ]; then "
        "  pkill -f mp_power_cpu_load_worker >/dev/null 2>&1 || true; "
        "  pkill -f mp_power_cpu_load\\.pids >/dev/null 2>&1 || true; "
        "fi; "
        "echo stopped"
    )
    rc, out, err = adb_shell(adb, serial, ["sh", "-c", script], timeout_s=60.0)
    if rc != 0:
        raise RuntimeError((err or out).strip() or "failed to stop cpu load")
    print("CPU load: stopped")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fixed pipeline: sample -> enrich -> report")
    parser.add_argument("--python", default=None, help="Python executable (default: current interpreter)")
    parser.add_argument("--adb", default=None, help="adb path (optional)")
    parser.add_argument("--serial", default=None, help="Device serial (optional)")
    parser.add_argument("--scenario", default="S1", help="Scenario label")
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--thermal", action="store_true")
    parser.add_argument("--display", action="store_true", help="Also sample display state via dumpsys power")
    parser.add_argument(
        "--batteryproperties",
        action="store_true",
        help="Also sample dumpsys batteryproperties (current_now/current_average/energy_counter if available)",
    )
    parser.add_argument(
        "--policy-knobs",
        action="store_true",
        help="Also sample policy knobs (cpufreq min/max/governor, cpuset, uclamp) to detect boost/throttle windows.",
    )
    parser.add_argument(
        "--policy-knobs-period-s",
        type=float,
        default=0.0,
        help="If --policy-knobs, sample knobs at most once per this period (seconds) and reuse last values (default: 0 = every sample).",
    )
    parser.add_argument(
        "--policy-services",
        default="",
        help=(
            "Comma-separated dumpsys services to sample for explicit policy state (e.g. "
            "SchedBoostService,miui.whetstone.power,performance_hint,power). "
            "This is a non-guess signal source; sample rate controlled by --policy-services-period-s."
        ),
    )
    parser.add_argument(
        "--policy-services-period-s",
        type=float,
        default=30.0,
        help="If --policy-services, sample services at most once per this period (seconds) and reuse last values.",
    )
    parser.add_argument(
        "--batterystats-usage",
        action="store_true",
        help="Use `dumpsys batterystats --usage --model power-profile` as a stable short-window energy estimate: reset before sampling and dump after.",
    )

    parser.add_argument(
        "--cpu-load-threads",
        type=int,
        default=0,
        help=(
            "If >0, start N simple busy-loop CPU workers on device for the whole run window and stop them after. "
            "Useful for S3 CPU负载实验. (best-effort, no root)"
        ),
    )

    # Optional: automatically set brightness/timeout for S2 (requires OEM allowing WRITE_SETTINGS).
    parser.add_argument(
        "--set-brightness",
        type=int,
        default=None,
        help="If provided, try to set manual brightness (0-255) via adb before sampling (best-effort).",
    )
    parser.add_argument(
        "--set-timeout-ms",
        type=int,
        default=None,
        help="If provided, try to set screen_off_timeout (ms) via adb before sampling (best-effort).",
    )
    parser.add_argument(
        "--enable-write-settings",
        action="store_true",
        help="Try `appops set com.android.shell WRITE_SETTINGS allow` so adb can change system settings.",
    )

    parser.add_argument(
        "--qc",
        action="store_true",
        help="After enrich, run qc/qc_run.py on the enriched CSV (prints quick acceptance stats).",
    )
    parser.add_argument(
        "--batterystats-proto",
        action="store_true",
        help=(
            "Dump `dumpsys batterystats --proto` (binary) before/after the run and parse a minimal schema "
            "to extract coulomb-counter discharge totals (mAh) + timing fields. "
            "This avoids instantaneous charge_counter differencing when current_now is unavailable."
        ),
    )
    parser.add_argument(
        "--batterystats-proto-reset",
        action="store_true",
        help="If set, run `dumpsys batterystats --reset` before capturing the START proto.",
    )
    perf = parser.add_mutually_exclusive_group()
    perf.add_argument(
        "--perfetto-android-power",
        dest="perfetto_android_power",
        action="store_true",
        default=True,
        help=(
            "Record a Perfetto trace using data source android.power (battery counters) during sampling, "
            "then parse batt.current_ua / batt.voltage_uv / batt.charge_uah into a timeseries + summary. "
            "(default: enabled)"
        ),
    )
    perf.add_argument(
        "--no-perfetto-android-power",
        dest="perfetto_android_power",
        action="store_false",
        help="Disable Perfetto android.power battery counters (not recommended; may cause inconsistent energy source).",
    )
    parser.add_argument(
        "--perfetto-battery-poll-ms",
        type=int,
        default=250,
        help="Perfetto android.power battery polling period (ms).",
    )
    parser.add_argument(
        "--perfetto-policy-trace",
        action="store_true",
        help=(
            "Also record a Perfetto trace with linux.ftrace + atrace categories to detect policy/scheduler events "
            "(cpu_frequency/cpu_idle/sched + optional vendor atrace markers). "
            "Produces a best-effort policy markers CSV from slices." 
        ),
    )
    parser.add_argument(
        "--perfetto-policy-atrace-categories",
        default="power,sched,freq,idle,thermal,am,wm",
        help="Comma-separated atrace categories to enable when --perfetto-policy-trace.",
    )
    parser.add_argument("--auto-reset-battery", action="store_true")
    parser.add_argument("--log-every", type=float, default=60.0, help="Sampler progress log period seconds")
    parser.add_argument(
        "--xmltree",
        type=Path,
        default=Path("artifacts/android/overlays/FrameworkResOverlay_power_profile_xmltree.txt"),
        help="power_profile xmltree path",
    )
    parser.add_argument(
        "--profile-out-dir",
        type=Path,
        default=Path("artifacts/android/power_profile"),
        help="output dir for power_profile.json + cluster tables",
    )
    parser.add_argument(
        "--map-json",
        type=Path,
        default=Path("artifacts/android/power_profile/policy_cluster_map.json"),
        help="policy->cluster mapping json",
    )
    parser.add_argument("--skip-sample", action="store_true", help="Skip sampling and only enrich/report")
    parser.add_argument("--run-csv", type=Path, default=None, help="Existing run CSV when --skip-sample")
    args = parser.parse_args()

    if args.set_brightness is not None and not (0 <= int(args.set_brightness) <= 255):
        raise SystemExit("--set-brightness must be in [0, 255]")
    if args.set_timeout_ms is not None and int(args.set_timeout_ms) <= 0:
        raise SystemExit("--set-timeout-ms must be positive")

    py = args.python
    if py is None:
        import sys

        py = sys.executable

    # 1) Ensure power_profile parsed (and includes optional items_ma for screen estimate)
    pp_json = args.profile_out_dir / "power_profile.json"
    need_parse = not pp_json.exists()
    if not need_parse:
        try:
            txt = pp_json.read_text(encoding="utf-8", errors="replace")
            if '"items_ma"' not in txt:
                need_parse = True
        except Exception:
            need_parse = True

    if need_parse:
        try:
            profile = parse_power_profile_xmltree(args.xmltree)
            write_power_profile_outputs(profile, args.profile_out_dir)
        except Exception as e:
            raise SystemExit(f"parse_power_profile_overlay failed: {e}")

    # 2) Ensure policy mapping
    if not args.map_json.exists():
        # map_policy_to_cluster expects an adb path; if not provided, hope it's on PATH.
        map_cmd = [py, "policy/map_policy_to_cluster.py"]
        if args.adb:
            map_cmd += ["--adb", args.adb]
        if args.serial:
            map_cmd += ["--serial", args.serial]
        rc, out, err = _run(map_cmd)
        if rc != 0:
            raise SystemExit(f"map_policy_to_cluster failed: {err or out}")

    # 3) Sample
    run_csv: Path
    if args.skip_sample:
        if args.run_csv is None:
            raise SystemExit("--skip-sample requires --run-csv")
        run_csv = args.run_csv
    else:
        run_id = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        run_csv = Path("artifacts") / "runs" / f"{run_id}_{args.scenario}.csv"
        run_csv.parent.mkdir(parents=True, exist_ok=True)

        # Pre-create report dir so we can stash raw proto dumps even before sampling.
        report_dir = Path("artifacts") / "reports" / f"{run_id}_{args.scenario}_enriched"
        report_dir.mkdir(parents=True, exist_ok=True)

        # Optional: Perfetto trace(s) (run in parallel with sampling).
        adb_path = resolve_adb(args.adb)
        serial_used = args.serial
        if not serial_used:
            serial_used = pick_default_serial(adb_path, timeout_s=8.0)
            if not serial_used:
                raise SystemExit("No adb devices found. Provide --serial or connect a device.")
            print(f"Using default adb serial: {serial_used}")
        perfetto_proc: subprocess.Popen[bytes] | None = None
        perfetto_remote_cfg: str | None = None
        perfetto_remote_out: str | None = None
        perfetto_local_trace: Path | None = None

        want_perfetto = bool(args.perfetto_android_power or args.perfetto_policy_trace)
        if want_perfetto:
            if args.perfetto_android_power:
                print(f"Perfetto android.power: enabled (battery_poll_ms={int(args.perfetto_battery_poll_ms)})")
            if args.perfetto_policy_trace:
                print("Perfetto policy trace: enabled (linux.ftrace + atrace)")

            if not args.serial:
                # Keep behavior consistent with other adb operations: if not provided, let adb decide.
                # However, perfetto tracing is critical enough that we require explicit serial if multiple devices.
                pass

            duration_ms = int(round(float(args.duration) * 1000.0))
            poll_ms = int(args.perfetto_battery_poll_ms)
            if args.perfetto_android_power and poll_ms <= 0:
                raise SystemExit("--perfetto-battery-poll-ms must be > 0")

            # Write pbtxt locally for audit/repro, but DO NOT push to device.
            # Some devices enforce SELinux rules that prevent the perfetto process from opening
            # config files under /data/local/tmp (errno=13). Feeding config via stdin avoids this.
            cfg_lines: list[str] = []
            cfg_lines.append(f"duration_ms: {duration_ms}")
            # Slightly larger buffer helps when enabling ftrace.
            cfg_lines.append("buffers: {\n  size_kb: 8192\n  fill_policy: RING_BUFFER\n}")

            if args.perfetto_android_power:
                cfg_lines.append("data_sources: {\n  config {\n    name: \"android.power\"\n    android_power_config {")
                cfg_lines.append(f"      battery_poll_ms: {poll_ms}")
                # NOTE: Field name is 'battery_counters' (not 'counters') in AndroidPowerConfig.
                cfg_lines.append("      battery_counters: BATTERY_COUNTER_CAPACITY_PERCENT")
                cfg_lines.append("      battery_counters: BATTERY_COUNTER_CHARGE")
                cfg_lines.append("      battery_counters: BATTERY_COUNTER_CURRENT")
                cfg_lines.append("      battery_counters: BATTERY_COUNTER_VOLTAGE")
                cfg_lines.append("    }\n  }\n}")

            if args.perfetto_policy_trace:
                cats = [c.strip() for c in str(args.perfetto_policy_atrace_categories).split(",") if c.strip()]
                cfg_lines.append("data_sources: {\n  config {\n    name: \"linux.ftrace\"\n    ftrace_config {")
                # Core events for frequency/idle/scheduler changes.
                for ev in [
                    "power/cpu_frequency",
                    "power/cpu_idle",
                    "sched/sched_switch",
                    "sched/sched_wakeup",
                    "sched/sched_wakeup_new",
                ]:
                    cfg_lines.append(f"      ftrace_events: \"{ev}\"")
                # Atrace categories (if supported) sometimes expose PowerHAL / vendor markers.
                for cat in cats:
                    cfg_lines.append(f"      atrace_categories: \"{cat}\"")
                cfg_lines.append('      atrace_apps: "*"')
                cfg_lines.append("    }\n  }\n}")

            cfg_text = "\n".join(cfg_lines) + "\n"

            local_cfg = report_dir / "perfetto_trace.pbtxt"
            local_cfg.write_text(cfg_text, encoding="utf-8")

            perfetto_remote_cfg = None
            perfetto_remote_out = f"/data/misc/perfetto-traces/mp_power_trace_{run_id}_{args.scenario}.pftrace"

            # Start perfetto in parallel.
            perfetto_cmd = [adb_path]
            if serial_used:
                perfetto_cmd += ["-s", serial_used]
            perfetto_cmd += [
                "shell",
                "perfetto",
                "--txt",
                "-c",
                "-",
                "-o",
                perfetto_remote_out,
            ]
            perfetto_proc = subprocess.Popen(
                perfetto_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if perfetto_proc.stdin is None:
                raise SystemExit("failed to open stdin pipe for perfetto")
            perfetto_proc.stdin.write(cfg_text.encode("utf-8"))
            perfetto_proc.stdin.close()
            print(f"Perfetto: started (remote_out={perfetto_remote_out})")

        # Optional: batterystats proto capture (schema-min). This is a binary blob: use exec-out.
        bs_start_pb: Path | None = None
        bs_end_pb: Path | None = None
        if args.batterystats_proto:
            if args.batterystats_proto_reset:
                rc, out, err = adb_shell(adb_path, serial_used, ["dumpsys", "batterystats", "--reset"], timeout_s=20.0)
                if rc != 0:
                    raise SystemExit(f"batterystats --reset failed: {err or out}")

            bs_start_pb = report_dir / "batterystats_start.pb"
            rc, blob, err = adb_exec_out(adb_path, serial_used, ["dumpsys", "batterystats", "--proto"], timeout_s=30.0)
            if rc != 0:
                raise SystemExit(f"batterystats --proto (start) failed: {err}")
            bs_start_pb.write_bytes(blob)

        # Optional: reset batterystats so the subsequent --usage dump represents only this run window.
        if args.batterystats_usage:
            rc, out, err = adb_shell(adb_path, serial_used, ["dumpsys", "batterystats", "--reset"], timeout_s=20.0)
            if rc != 0:
                raise SystemExit(f"batterystats --reset failed: {err or out}")

        # Optional: set brightness/timeout before sampling (useful for S2 brightness steps).
        if args.set_brightness is not None or args.set_timeout_ms is not None:
            try:
                if args.enable_write_settings:
                    _ensure_write_settings(adb_path, serial_used)

                if args.set_brightness is not None:
                    # Enforce manual mode for rigorous S2.
                    _set_system_setting(adb_path, serial_used, "screen_brightness_mode", 0)
                    _set_system_setting(adb_path, serial_used, "screen_brightness", int(args.set_brightness))

                if args.set_timeout_ms is not None:
                    _set_system_setting(adb_path, serial_used, "screen_off_timeout", int(args.set_timeout_ms))

                b = _get_system_setting(adb_path, serial_used, "screen_brightness")
                m = _get_system_setting(adb_path, serial_used, "screen_brightness_mode")
                t = _get_system_setting(adb_path, serial_used, "screen_off_timeout")
                print(f"Applied settings: screen_brightness={b} mode={m} timeout_ms={t}")
            except Exception as e:
                raise SystemExit(
                    "Failed to set brightness/timeout via adb. "
                    "If your device restricts WRITE_SETTINGS, try adding --enable-write-settings, "
                    "or set brightness/timeout manually in the phone UI. "
                    f"Details: {e}"
                )

        cpu_load_started = False
        try:
            if args.cpu_load_threads and int(args.cpu_load_threads) > 0:
                _cpu_load_start(adb_path, serial_used, int(args.cpu_load_threads))
                cpu_load_started = True

            sample_cmd = [
                py,
                "scripts/adb_sample_power.py",
                "--scenario",
                args.scenario,
                "--duration",
                str(args.duration),
                "--interval",
                str(args.interval),
                "--out",
                str(run_csv),
                "--log-every",
                str(args.log_every),
            ]
            if args.adb:
                sample_cmd += ["--adb", args.adb]
            # Always pass a serial when multiple devices may exist.
            if serial_used:
                sample_cmd += ["--serial", serial_used]
            if args.thermal:
                sample_cmd += ["--thermal"]
            if args.display:
                sample_cmd += ["--display"]
            if args.batteryproperties:
                sample_cmd += ["--batteryproperties"]
            if args.policy_knobs:
                sample_cmd += ["--policy-knobs"]
                if args.policy_knobs_period_s and float(args.policy_knobs_period_s) > 0:
                    sample_cmd += ["--policy-knobs-period-s", str(args.policy_knobs_period_s)]

            if str(getattr(args, "policy_services", "")).strip():
                sample_cmd += ["--policy-services", str(args.policy_services)]
                if args.policy_services_period_s and float(args.policy_services_period_s) > 0:
                    sample_cmd += ["--policy-services-period-s", str(args.policy_services_period_s)]
            if args.auto_reset_battery:
                sample_cmd += ["--auto-reset-battery"]

            # Passthrough stdout/stderr so long runs keep producing output (avoids appearing idle).
            rc = subprocess.run(sample_cmd).returncode
            if rc != 0:
                raise SystemExit(f"adb_sample_power failed with code {rc}")

            # Wait for perfetto to finish and pull + parse trace.
            if want_perfetto:
                if perfetto_proc is None or perfetto_remote_out is None:
                    raise SystemExit("perfetto process was not started")

                try:
                    # Give perfetto a small grace period after sampling ends.
                    stdout_b, stderr_b = perfetto_proc.communicate(timeout=float(args.duration) + 30.0)
                except subprocess.TimeoutExpired:
                    perfetto_proc.kill()
                    raise SystemExit("perfetto did not finish in time")

                if perfetto_proc.returncode not in (0, None):
                    stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
                    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
                    raise SystemExit(f"perfetto failed (exit={perfetto_proc.returncode}): {stderr or stdout}")

                perfetto_local_trace = report_dir / "perfetto_trace.pftrace"
                rc, blob, err = adb_exec_out(adb_path, serial_used, ["cat", perfetto_remote_out], timeout_s=30.0)
                if rc != 0:
                    raise SystemExit(f"failed to pull perfetto trace via exec-out: {err}")
                perfetto_local_trace.write_bytes(blob)
                if perfetto_local_trace.stat().st_size == 0:
                    raise SystemExit("perfetto trace is empty")
                print(f"Perfetto: pulled trace -> {perfetto_local_trace}")

                if args.perfetto_android_power:
                    try:
                        parse_perfetto_android_power_counters(
                            perfetto_local_trace,
                            out_dir=report_dir,
                            label=f"{run_id}_{args.scenario}",
                            no_timeseries=False,
                        )
                    except Exception as e:
                        raise SystemExit(f"parse_perfetto_android_power_counters failed: {e}")
                    print("Perfetto: parsed android.power -> perfetto_android_power_summary.csv + timeseries.csv")

                if args.perfetto_policy_trace:
                    try:
                        parse_perfetto_policy_markers(perfetto_local_trace, out_dir=report_dir)
                    except Exception as e:
                        raise SystemExit(f"parse_perfetto_policy_markers failed: {e}")

                # Best-effort cleanup.
                adb_shell(adb_path, serial_used, ["rm", "-f", perfetto_remote_out], timeout_s=10.0)

            # Capture END proto after sampling (before enrich/report is fine).
            if args.batterystats_proto:
                bs_end_pb = report_dir / "batterystats_end.pb"
                rc, blob, err = adb_exec_out(adb_path, serial_used, ["dumpsys", "batterystats", "--proto"], timeout_s=30.0)
                if rc != 0:
                    raise SystemExit(f"batterystats --proto (end) failed: {err}")
                bs_end_pb.write_bytes(blob)
        finally:
            if cpu_load_started:
                try:
                    _cpu_load_stop(adb_path, serial_used)
                except Exception:
                    # best-effort cleanup; do not mask primary errors
                    pass
            if want_perfetto and perfetto_proc is not None and perfetto_proc.poll() is None:
                # Avoid leaving perfetto running if sampling failed.
                try:
                    perfetto_proc.kill()
                except Exception:
                    pass

    # 4) Enrich
    enriched_csv = run_csv.with_name(run_csv.stem + "_enriched.csv")
    try:
        enrich_run_with_cpu_energy(run_csv=run_csv, out_csv=enriched_csv)
    except Exception as e:
        raise SystemExit(f"enrich_run_with_cpu_energy failed: {e}")

    # 4.5) Optional QC
    if args.qc:
        qc_cmd = [py, "qc/qc_run.py", "--csv", str(enriched_csv)]
        rc = subprocess.run(qc_cmd).returncode
        if rc != 0:
            raise SystemExit(f"qc_run failed with code {rc}")

    # 5) Report
    try:
        report_run(enriched_csv)
    except Exception as e:
        raise SystemExit(f"report_run failed: {e}")

    # 5.5) Optional: parse batterystats proto (schema-min) into JSON/CSV
    if args.batterystats_proto and not args.skip_sample:
        # Recompute report_dir here for clarity.
        report_dir = Path("artifacts") / "reports" / enriched_csv.stem
        report_dir.mkdir(parents=True, exist_ok=True)

        start_pb = report_dir / "batterystats_start.pb"
        end_pb = report_dir / "batterystats_end.pb"

        # If we captured earlier into the same report_dir naming, these will exist.
        # Otherwise, fall back to legacy naming created before enrich.
        if not start_pb.exists():
            legacy_dir = Path("artifacts") / "reports" / f"{enriched_csv.stem.removesuffix('_enriched')}_enriched"
            legacy_start = legacy_dir / "batterystats_start.pb"
            legacy_end = legacy_dir / "batterystats_end.pb"
            if legacy_start.exists():
                start_pb.write_bytes(legacy_start.read_bytes())
            if legacy_end.exists():
                end_pb.write_bytes(legacy_end.read_bytes())

        if start_pb.exists() and end_pb.exists() and end_pb.stat().st_size > 0:
            out_json = report_dir / "batterystats_proto_min_summary.json"
            out_csv = report_dir / "batterystats_proto_min_summary.csv"
            try:
                write_batterystats_min_summary(
                    start_pb=start_pb,
                    end_pb=end_pb,
                    out_json=out_json,
                    out_csv=out_csv,
                    label=args.scenario,
                )
            except Exception as e:
                raise SystemExit(f"parse_batterystats_proto_min failed: {e}")
        else:
            print("WARN: batterystats proto dumps missing or empty; skipping proto parse")

    # 6) Optional: batterystats usage dump + parsed summary
    if args.batterystats_usage and not args.skip_sample:
        adb_path = resolve_adb(args.adb)
        rc, out, err = adb_shell(
            adb_path,
            args.serial,
            ["dumpsys", "batterystats", "--usage", "--model", "power-profile"],
            timeout_s=60.0,
        )
        if rc != 0:
            raise SystemExit(f"batterystats --usage failed: {err or out}")

        report_dir = Path("artifacts") / "reports" / enriched_csv.stem
        report_dir.mkdir(parents=True, exist_ok=True)
        raw_path = report_dir / "batterystats_usage.txt"
        raw_path.write_text(out, encoding="utf-8")

        global_mah = _parse_batterystats_usage_global(out)
        if global_mah:
            # Write a tiny CSV for easy plotting/compare.
            summary_path = report_dir / "batterystats_usage_global_mAh.csv"
            lines = ["component,mAh"]
            for k in sorted(global_mah.keys()):
                lines.append(f"{k},{global_mah[k]}")
            summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"Wrote: {raw_path}")
            print(f"Wrote: {summary_path}")
        else:
            print(f"Wrote: {raw_path}")
            print("WARN: Could not parse Global section from batterystats --usage output")

    print(f"Run CSV: {run_csv}")
    print(f"Enriched: {enriched_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
