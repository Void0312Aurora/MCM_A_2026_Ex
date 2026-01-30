from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


@dataclass
class RunSummary:
    start_ts: str
    end_ts: str
    duration_s: float
    rows: int
    mean_voltage_mv: float | None
    mean_batt_power_mw: float | None
    mean_cpu_power_mw: float | None


def _parse_ts(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "ts_pc" in out.columns:
        ts = out["ts_pc"].astype(str).map(_parse_ts)
        out["t_s"] = (ts - ts.iloc[0]).dt.total_seconds() if hasattr(ts, "dt") else None

    # Ensure numeric columns
    for col in ["battery_voltage_mv", "charge_counter_uAh", "cpu_energy_mJ_total", "brightness"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    if "battery_voltage_mv" in out.columns and "charge_counter_uAh" in out.columns and "t_s" in out.columns:
        d_uah = out["charge_counter_uAh"].diff()
        dt_s = out["t_s"].diff()
        # discharge current positive when charge counter decreases
        out["batt_discharge_current_mA"] = (-d_uah * 3.6) / dt_s
        out["batt_discharge_power_mW"] = out["batt_discharge_current_mA"] * out["battery_voltage_mv"] / 1000.0

    # CPU average power (mW) from energy per interval (mJ) / dt (s)
    if "cpu_energy_mJ_total" in out.columns:
        if "t_s" in out.columns:
            dt_s = out["t_s"].diff()
            out["cpu_power_mW_total"] = out["cpu_energy_mJ_total"] / dt_s
        elif "dt_s" in out.columns:
            out["dt_s"] = pd.to_numeric(out["dt_s"], errors="coerce")
            out["cpu_power_mW_total"] = out["cpu_energy_mJ_total"] / out["dt_s"]

    return out


def summarize(df: pd.DataFrame) -> RunSummary:
    rows = int(len(df))
    start_ts = str(df["ts_pc"].iloc[0]) if "ts_pc" in df.columns and rows else ""
    end_ts = str(df["ts_pc"].iloc[-1]) if "ts_pc" in df.columns and rows else ""

    duration_s = float(df["t_s"].iloc[-1]) if "t_s" in df.columns and rows else float("nan")

    mean_voltage_mv = float(df["battery_voltage_mv"].mean()) if "battery_voltage_mv" in df.columns else None
    mean_batt_power_mw = float(df["batt_discharge_power_mW"].mean()) if "batt_discharge_power_mW" in df.columns else None
    mean_cpu_power_mw = float(df["cpu_power_mW_total"].mean()) if "cpu_power_mW_total" in df.columns else None

    return RunSummary(
        start_ts=start_ts,
        end_ts=end_ts,
        duration_s=duration_s,
        rows=rows,
        mean_voltage_mv=mean_voltage_mv,
        mean_batt_power_mw=mean_batt_power_mw,
        mean_cpu_power_mw=mean_cpu_power_mw,
    )


def write_markdown(summary: RunSummary, out_path: Path, source_csv: Path) -> None:
    lines: list[str] = []
    lines.append(f"# Run report")
    lines.append("")
    lines.append(f"- source: {source_csv.as_posix()}")
    lines.append(f"- rows: {summary.rows}")
    if summary.start_ts:
        lines.append(f"- start: {summary.start_ts}")
    if summary.end_ts:
        lines.append(f"- end: {summary.end_ts}")
    if summary.duration_s == summary.duration_s:
        lines.append(f"- duration_s: {summary.duration_s:.1f}")
    if summary.mean_voltage_mv is not None:
        lines.append(f"- mean_voltage_mv: {summary.mean_voltage_mv:.1f}")
    if summary.mean_batt_power_mw is not None:
        lines.append(f"- mean_batt_discharge_power_mW: {summary.mean_batt_power_mw:.1f}")
    if summary.mean_cpu_power_mw is not None:
        lines.append(f"- mean_cpu_power_mW_total: {summary.mean_cpu_power_mw:.1f}")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def plot(df: pd.DataFrame, out_path: Path) -> None:
    cols = df.columns

    fig, axes = plt.subplots(4, 1, figsize=(11, 14), sharex=True)

    x = df["t_s"] if "t_s" in cols else range(len(df))

    # 1) Battery voltage
    if "battery_voltage_mv" in cols:
        axes[0].plot(x, df["battery_voltage_mv"], label="battery_voltage_mv")
        axes[0].set_ylabel("mV")
        axes[0].legend(loc="best")

    # 2) Thermal
    thermal_cols = [c for c in cols if c.startswith("thermal_") and c.endswith("_C")]
    if thermal_cols:
        for c in thermal_cols:
            axes[1].plot(x, df[c], label=c)
        axes[1].set_ylabel("C")
        axes[1].legend(loc="best", ncols=2)

    # 3) CPU power
    if "cpu_power_mW_total" in cols:
        axes[2].plot(x, df["cpu_power_mW_total"], label="cpu_power_mW_total")
        axes[2].set_ylabel("mW")
        axes[2].legend(loc="best")

    # 4) Brightness / discharge power
    if "brightness" in cols:
        axes[3].plot(x, df["brightness"], label="brightness", color="tab:orange")
    if "batt_discharge_power_mW" in cols:
        axes[3].plot(x, df["batt_discharge_power_mW"], label="batt_discharge_power_mW", color="tab:green")
    axes[3].set_ylabel("brightness / mW")
    axes[3].legend(loc="best")

    axes[-1].set_xlabel("t (s)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a quick report (markdown + plots) for a run CSV")
    parser.add_argument("--csv", type=Path, required=True, help="Input run CSV (prefer enriched)")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: artifacts/reports/<csv_stem>/)",
    )
    args = parser.parse_args()

    csv_path: Path = args.csv
    out_dir = args.out_dir if args.out_dir is not None else Path("artifacts") / "reports" / csv_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(csv_path)
    df = add_derived_columns(df)

    s = summarize(df)

    md_path = out_dir / "summary.md"
    png_path = out_dir / "timeseries.png"

    write_markdown(s, md_path, csv_path)
    plot(df, png_path)

    print(f"Wrote: {md_path}")
    print(f"Wrote: {png_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
