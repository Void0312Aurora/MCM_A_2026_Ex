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
        if args.auto_reset_battery:
            sample_cmd += ["--auto-reset-battery"]

        # Passthrough stdout/stderr so long runs keep producing output (avoids appearing idle).
        rc = subprocess.run(sample_cmd).returncode
        if rc != 0:
            raise SystemExit(f"adb_sample_power failed with code {rc}")

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

    print(f"Run CSV: {run_csv}")
    print(f"Enriched: {enriched_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
