from __future__ import annotations

import argparse
import re
import subprocess
from datetime import datetime
from pathlib import Path


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


def _adb_shell(adb: str, serial: str | None, args: list[str], timeout_s: float | None = None) -> tuple[int, str, str]:
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["shell", *args]
    return _run(cmd, timeout_s=timeout_s)


def _adb_exec_out(adb: str, serial: str | None, args: list[str], timeout_s: float | None = None) -> tuple[int, bytes, str]:
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["exec-out", *args]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_s)
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", errors="replace")


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
        "--batterystats-usage",
        action="store_true",
        help="Use `dumpsys batterystats --usage --model power-profile` as a stable short-window energy estimate: reset before sampling and dump after.",
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
    parser.add_argument(
        "--perfetto-android-power",
        action="store_true",
        help=(
            "Record a Perfetto trace using data source android.power (battery counters) during sampling, "
            "then parse batt.current_ua / batt.voltage_uv / batt.charge_uah into a timeseries + summary. "
            "Works on devices which implement these counters." 
        ),
    )
    parser.add_argument(
        "--perfetto-battery-poll-ms",
        type=int,
        default=250,
        help="Perfetto android.power battery polling period (ms).",
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

    py = args.python
    if py is None:
        import sys

        py = sys.executable

    # 1) Ensure power_profile parsed
    if not (args.profile_out_dir / "power_profile.json").exists():
        cmd = [py, "scripts/parse_power_profile_overlay.py", str(args.xmltree), "--out-dir", str(args.profile_out_dir)]
        rc, out, err = _run(cmd)
        if rc != 0:
            raise SystemExit(f"parse_power_profile_overlay failed: {err or out}")

    # 2) Ensure policy mapping
    if not args.map_json.exists():
        # map_policy_to_cluster expects an adb path; if not provided, hope it's on PATH.
        map_cmd = [py, "scripts/map_policy_to_cluster.py"]
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

        # Optional: Perfetto android.power battery counters trace (run in parallel with sampling).
        adb_path = args.adb or "adb"
        perfetto_proc: subprocess.Popen[bytes] | None = None
        perfetto_remote_cfg: str | None = None
        perfetto_remote_out: str | None = None
        perfetto_local_trace: Path | None = None

        if args.perfetto_android_power:
            if not args.serial:
                # Keep behavior consistent with other adb operations: if not provided, let adb decide.
                # However, perfetto tracing is critical enough that we require explicit serial if multiple devices.
                pass

            duration_ms = int(round(float(args.duration) * 1000.0))
            poll_ms = int(args.perfetto_battery_poll_ms)
            if poll_ms <= 0:
                raise SystemExit("--perfetto-battery-poll-ms must be > 0")

            # Write pbtxt locally for audit/repro, but DO NOT push to device.
            # Some devices enforce SELinux rules that prevent the perfetto process from opening
            # config files under /data/local/tmp (errno=13). Feeding config via stdin avoids this.
            cfg_text = (
                "\n".join(
                    [
                        f"duration_ms: {duration_ms}",
                        "buffers: {\n  size_kb: 2048\n  fill_policy: RING_BUFFER\n}",
                        "data_sources: {\n  config {\n    name: \"android.power\"\n    android_power_config {",
                        f"      battery_poll_ms: {poll_ms}",
                        # NOTE: Field name is 'battery_counters' (not 'counters') in AndroidPowerConfig.
                        "      battery_counters: BATTERY_COUNTER_CAPACITY_PERCENT",
                        "      battery_counters: BATTERY_COUNTER_CHARGE",
                        "      battery_counters: BATTERY_COUNTER_CURRENT",
                        "      battery_counters: BATTERY_COUNTER_VOLTAGE",
                        "    }\n  }\n}",
                    ]
                )
                + "\n"
            )
            local_cfg = report_dir / "perfetto_android_power.pbtxt"
            local_cfg.write_text(cfg_text, encoding="utf-8")

            perfetto_remote_cfg = None
            perfetto_remote_out = f"/data/misc/perfetto-traces/mp_power_android_power_{run_id}_{args.scenario}.pftrace"

            # Start perfetto in parallel.
            perfetto_cmd = [adb_path]
            if args.serial:
                perfetto_cmd += ["-s", args.serial]
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

        # Optional: batterystats proto capture (schema-min). This is a binary blob: use exec-out.
        bs_start_pb: Path | None = None
        bs_end_pb: Path | None = None
        if args.batterystats_proto:
            if args.batterystats_proto_reset:
                rc, out, err = _adb_shell(adb_path, args.serial, ["dumpsys", "batterystats", "--reset"], timeout_s=20.0)
                if rc != 0:
                    raise SystemExit(f"batterystats --reset failed: {err or out}")

            bs_start_pb = report_dir / "batterystats_start.pb"
            rc, blob, err = _adb_exec_out(adb_path, args.serial, ["dumpsys", "batterystats", "--proto"], timeout_s=30.0)
            if rc != 0:
                raise SystemExit(f"batterystats --proto (start) failed: {err}")
            bs_start_pb.write_bytes(blob)

        # Optional: reset batterystats so the subsequent --usage dump represents only this run window.
        if args.batterystats_usage:
            # If adb not provided, assume on PATH.
            adb_path = args.adb or "adb"
            rc, out, err = _adb_shell(adb_path, args.serial, ["dumpsys", "batterystats", "--reset"], timeout_s=20.0)
            if rc != 0:
                raise SystemExit(f"batterystats --reset failed: {err or out}")

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
        if args.serial:
            sample_cmd += ["--serial", args.serial]
        if args.thermal:
            sample_cmd += ["--thermal"]
        if args.display:
            sample_cmd += ["--display"]
        if args.batteryproperties:
            sample_cmd += ["--batteryproperties"]
        if args.auto_reset_battery:
            sample_cmd += ["--auto-reset-battery"]

        # Passthrough stdout/stderr so long runs keep producing output (avoids appearing idle).
        rc = subprocess.run(sample_cmd).returncode
        if rc != 0:
            raise SystemExit(f"adb_sample_power failed with code {rc}")

        # Wait for perfetto to finish and pull + parse trace.
        if args.perfetto_android_power:
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

            perfetto_local_trace = report_dir / "perfetto_android_power.pftrace"
            rc, blob, err = _adb_exec_out(adb_path, args.serial, ["cat", perfetto_remote_out], timeout_s=30.0)
            if rc != 0:
                raise SystemExit(f"failed to pull perfetto trace via exec-out: {err}")
            perfetto_local_trace.write_bytes(blob)
            if perfetto_local_trace.stat().st_size == 0:
                raise SystemExit("perfetto trace is empty")

            # Parse battery counters into CSV/JSON.
            parse_cmd = [
                py,
                "scripts/parse_perfetto_android_power_counters.py",
                "--trace",
                str(perfetto_local_trace),
                "--out-dir",
                str(report_dir),
                "--label",
                f"{run_id}_{args.scenario}",
            ]
            rc, out, err = _run(parse_cmd)
            if rc != 0:
                raise SystemExit(f"parse_perfetto_android_power_counters failed: {err or out}")

            # Best-effort cleanup.
            _adb_shell(adb_path, args.serial, ["rm", "-f", perfetto_remote_out], timeout_s=10.0)

        # Capture END proto after sampling (before enrich/report is fine).
        if args.batterystats_proto:
            bs_end_pb = report_dir / "batterystats_end.pb"
            rc, blob, err = _adb_exec_out(adb_path, args.serial, ["dumpsys", "batterystats", "--proto"], timeout_s=30.0)
            if rc != 0:
                raise SystemExit(f"batterystats --proto (end) failed: {err}")
            bs_end_pb.write_bytes(blob)

    # 4) Enrich
    enriched_csv = run_csv.with_name(run_csv.stem + "_enriched.csv")
    enrich_cmd = [py, "scripts/enrich_run_with_cpu_energy.py", "--run-csv", str(run_csv), "--out", str(enriched_csv)]
    rc, out, err = _run(enrich_cmd)
    if rc != 0:
        raise SystemExit(f"enrich_run_with_cpu_energy failed: {err or out}")

    # 5) Report
    report_cmd = [py, "scripts/report_run.py", "--csv", str(enriched_csv)]
    rc, out, err = _run(report_cmd)
    if rc != 0:
        raise SystemExit(f"report_run failed: {err or out}")

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
            cmd = [
                py,
                "scripts/parse_batterystats_proto_min.py",
                "--start",
                str(start_pb),
                "--end",
                str(end_pb),
                "--out-json",
                str(out_json),
                "--out-csv",
                str(out_csv),
                "--label",
                args.scenario,
            ]
            rc, out, err = _run(cmd)
            if rc != 0:
                raise SystemExit(f"parse_batterystats_proto_min failed: {err or out}")
        else:
            print("WARN: batterystats proto dumps missing or empty; skipping proto parse")

    # 6) Optional: batterystats usage dump + parsed summary
    if args.batterystats_usage and not args.skip_sample:
        adb_path = args.adb or "adb"
        rc, out, err = _adb_shell(
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
