from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def _flag(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str):
        v = v.strip()
        if v == "":
            return False
    try:
        return bool(int(v))
    except Exception:
        return bool(v)


def _screen_mode(v) -> str:
    s = "" if v is None else str(v).strip().lower()
    if not s or s == "nan":
        return ""
    if s in ("on", "wake", "wakeup", "1", "true"):
        return "on"
    if s in ("off", "sleep", "0", "false"):
        return "off"
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate pipeline_run.py commands from a CSV test plan")
    ap.add_argument(
        "--plan",
        type=Path,
        default=Path("configs/test_plan_v2.csv"),
        help="CSV file describing planned runs",
    )
    ap.add_argument(
        "--python",
        default="python",
        help="Python command to invoke (e.g., python or path/to/.venv/python.exe)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional output file path (e.g., artifacts/run_plan_v2.ps1)",
    )
    ap.add_argument(
        "--only-plan-id",
        default=None,
        help=(
            "Optional filter: only emit rows whose plan_id matches one of these (comma-separated). "
            "Example: --only-plan-id S3-GRAD"
        ),
    )
    ap.add_argument(
        "--only-scenario-prefix",
        default=None,
        help=(
            "Optional filter: only emit rows whose scenario starts with this prefix. "
            "Example: --only-scenario-prefix S3_"
        ),
    )
    ap.add_argument(
        "--encoding",
        default="utf-8-sig",
        help="Encoding for --out file (default: utf-8-sig for Windows PowerShell compatibility)",
    )
    args = ap.parse_args()

    df = pd.read_csv(args.plan, encoding="utf-8-sig")

    only_plan_ids: set[str] | None = None
    if args.only_plan_id is not None and str(args.only_plan_id).strip() != "":
        only_plan_ids = {p.strip() for p in str(args.only_plan_id).split(",") if p.strip()}

    scenario_prefix = None
    if args.only_scenario_prefix is not None and str(args.only_scenario_prefix).strip() != "":
        scenario_prefix = str(args.only_scenario_prefix).strip()

    cmds: list[str] = []
    for _, row in df.iterrows():
        scenario = str(row.get("scenario", "")).strip()
        if not scenario:
            continue

        plan_id = str(row.get("plan_id", "")).strip()
        if only_plan_ids is not None and plan_id not in only_plan_ids:
            continue
        if scenario_prefix is not None and not scenario.startswith(scenario_prefix):
            continue

        repeat = int(row.get("repeat", 1) or 1)
        duration = float(row.get("duration_s", 540) or 540)
        interval = float(row.get("interval_s", 2) or 2)

        thermal = _flag(row.get("thermal", 1))
        display = _flag(row.get("display", 1))
        qc = _flag(row.get("qc", 1))

        set_brightness = row.get("set_brightness", None)
        cpu_threads = row.get("cpu_load_threads", None)
        cpu_load_best_effort = _flag(row.get("cpu_load_best_effort", 0))
        screen_before = _screen_mode(row.get("screen_before", None))
        auto_reset_settings = _flag(row.get("auto_reset_settings", 0))

        notes = str(row.get("notes", "")).strip()

        for i in range(repeat):
            parts = [
                args.python,
                "scripts/pipeline_run.py",
                f"--scenario {scenario}",
                f"--duration {duration:g}",
                f"--interval {interval:g}",
            ]
            if thermal:
                parts.append("--thermal")
            if display:
                parts.append("--display")
            if qc:
                parts.append("--qc")

            if screen_before == "on":
                parts.append("--screen-wake-before")
            elif screen_before == "off":
                parts.append("--screen-sleep-before")

            # Brightness setting is best-effort; enable write-settings to increase chance.
            if set_brightness is not None and not pd.isna(set_brightness) and str(set_brightness).strip() != "":
                parts.append("--enable-write-settings")
                parts.append(f"--set-brightness {int(float(set_brightness))}")
                # Keep screen from timing out during S2.
                parts.append("--set-timeout-ms 2147483647")
                if auto_reset_settings:
                    parts.append("--auto-reset-settings")

            if cpu_threads is not None and not pd.isna(cpu_threads) and str(cpu_threads).strip() != "":
                threads = int(float(cpu_threads))
                if threads > 0:
                    parts.append(f"--cpu-load-threads {threads}")
                    if cpu_load_best_effort:
                        parts.append("--cpu-load-best-effort")

            cmd = " ".join(parts)
            if repeat > 1:
                cmd = f"# repeat {i+1}/{repeat}\n" + cmd
            if plan_id:
                cmd = f"# plan_id={plan_id}\n" + cmd
            if notes:
                cmd = f"# {notes}\n" + cmd
            cmds.append(cmd)

    output_text = "\n\n".join(cmds) + "\n"

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output_text, encoding=str(args.encoding), newline="\n")

    print(output_text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
