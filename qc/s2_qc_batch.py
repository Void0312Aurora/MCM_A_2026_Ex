"""Batch QC for S2 runs.

Purpose:
- Summarize each run into a single point (brightness, duration, discharge mAh, avg power)
- Mark obviously invalid/aborted runs (too few rows / too short duration)
- Optionally use batterystats-proto total discharge if present, otherwise charge_counter

Example:
    python qc/s2_qc_batch.py --runs artifacts/runs/*S2*_enriched.csv --out-dir artifacts/reports/S2_qc
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


@dataclass
class Point:
    label: str
    path: str
    brightness: int | None
    samples: int
    duration_s: float | None
    discharge_mAh_cc: float | None
    discharge_mAh_bs: float | None
    discharge_mAh_pf: float | None
    pf_state: str | None
    discharge_mAh_preferred: float | None
    preferred_source: str
    v_mean_V: float | None
    p_avg_W_preferred: float | None
    energy_mWh_pf: float | None
    is_valid: bool
    invalid_reason: str | None
    bs_quantization_rel_err_max: float | None


def _infer_label(df: pd.DataFrame, fallback: str) -> str:
    scenario = None
    if "scenario" in df.columns and len(df) > 0 and pd.notna(df.loc[0, "scenario"]):
        scenario = str(df.loc[0, "scenario"])
    label = scenario or fallback

    brightness = None
    if "brightness" in df.columns and df["brightness"].notna().any():
        try:
            brightness = int(pd.to_numeric(df["brightness"], errors="coerce").dropna().iloc[0])
        except Exception:
            brightness = None

    if brightness is not None and ("b" not in label):
        label = f"{label}_b{brightness}"
    return label


def _find_report_dir(run_csv: Path) -> Path | None:
    stem = run_csv.stem
    report_stem = stem if stem.endswith("_enriched") else stem + "_enriched"
    report_dir = Path("artifacts") / "reports" / report_stem
    if report_dir.exists():
        return report_dir
    return None


def _duration_s_from_ts(df: pd.DataFrame) -> float | None:
    if "ts_pc" not in df.columns or not df["ts_pc"].notna().any():
        return None
    ts = pd.to_datetime(df["ts_pc"], errors="coerce")
    if ts.notna().all():
        return float((ts.iloc[-1] - ts.iloc[0]).total_seconds())
    return None


def _discharge_mAh_from_charge_counter(df: pd.DataFrame) -> float | None:
    if "charge_counter_uAh" not in df.columns or not df["charge_counter_uAh"].notna().any():
        return None
    try:
        cc = pd.to_numeric(df["charge_counter_uAh"], errors="coerce").dropna().astype("int64")
        if len(cc) < 2:
            return None
        delta_uAh = int(cc.iloc[-1] - cc.iloc[0])
        return abs(delta_uAh) / 1000.0
    except Exception:
        return None


def _mean_voltage_V(df: pd.DataFrame) -> float | None:
    if "battery_voltage_mv" not in df.columns or not df["battery_voltage_mv"].notna().any():
        return None
    try:
        return float(pd.to_numeric(df["battery_voltage_mv"], errors="coerce").dropna().mean()) / 1000.0
    except Exception:
        return None


def _read_batterystats_delta_mAh(report_dir: Path | None) -> tuple[float | None, float | None]:
    """Returns (discharge_mAh_abs, quantization_rel_err_max)."""
    if report_dir is None:
        return None, None
    bs_csv = report_dir / "batterystats_proto_min_summary.csv"
    if not bs_csv.exists():
        return None, None

    bs = pd.read_csv(bs_csv)
    if "metric" not in bs.columns or "value" not in bs.columns:
        return None, None
    # Keep raw values; handle NaN/empty robustly in parser.
    m = dict(zip(bs["metric"].astype(str), bs["value"]))

    def f(key: str) -> float | None:
        s = m.get(key)
        if s is None:
            return None
        if isinstance(s, float) and pd.isna(s):
            return None
        s = str(s).strip()
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            return None

    delta_total_mah = f("delta_total_mah")
    discharge_abs = abs(delta_total_mah) if delta_total_mah is not None else None
    rel_err = None
    if discharge_abs is not None and discharge_abs > 0:
        rel_err = 0.5 / discharge_abs
    return discharge_abs, rel_err


def _read_perfetto_summary(report_dir: Path | None) -> tuple[float | None, float | None, float | None]:
    """Returns (discharge_mAh, p_avg_W, energy_mWh)."""
    if report_dir is None:
        return None, None, None
    pf_csv = report_dir / "perfetto_android_power_summary.csv"
    if not pf_csv.exists():
        return None, None, None
    try:
        pf = pd.read_csv(pf_csv)
        if pf.empty:
            return None, None, None
        r = pf.iloc[0]
        discharge = float(r["discharge_mah"]) if "discharge_mah" in pf.columns and pd.notna(r["discharge_mah"]) else None
        power_w = None
        if "power_mw_mean" in pf.columns and pd.notna(r["power_mw_mean"]):
            power_w = float(r["power_mw_mean"]) / 1000.0
        energy = float(r["energy_mwh"]) if "energy_mwh" in pf.columns and pd.notna(r["energy_mwh"]) else None
        return discharge, power_w, energy
    except Exception:
        return None, None, None


def _pf_state(discharge_mah: float | None) -> str | None:
    if discharge_mah is None:
        return None
    if discharge_mah > 0:
        return "discharging"
    if discharge_mah < 0:
        return "charging"
    return "flat"


def _avg_power_W(discharge_mAh: float | None, duration_s: float | None, v_mean_V: float | None) -> float | None:
    if discharge_mAh is None or duration_s is None or duration_s <= 0 or v_mean_V is None:
        return None
    I_A = (discharge_mAh / 1000.0) / (duration_s / 3600.0)
    return I_A * v_mean_V


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch QC table + plot for S2 runs")
    ap.add_argument("--runs", type=Path, nargs="+", required=True, help="Run CSV paths")
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts") / "reports" / "S2_qc")
    ap.add_argument("--prefer", choices=["perfetto", "batterystats", "charge_counter"], default="perfetto")
    ap.add_argument("--min-rows", type=int, default=30)
    ap.add_argument("--min-duration-s", type=float, default=60.0)
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    points: list[Point] = []
    for run_path in args.runs:
        df = pd.read_csv(run_path)
        label = _infer_label(df, run_path.stem)

        brightness = None
        if "brightness" in df.columns and df["brightness"].notna().any():
            try:
                brightness = int(pd.to_numeric(df["brightness"], errors="coerce").dropna().iloc[0])
            except Exception:
                brightness = None

        duration_s = _duration_s_from_ts(df)
        discharge_cc = _discharge_mAh_from_charge_counter(df)
        v_mean_V = _mean_voltage_V(df)

        report_dir = _find_report_dir(run_path)
        discharge_bs, bs_rel_err = _read_batterystats_delta_mAh(report_dir)
        discharge_pf, p_avg_pf, energy_pf = _read_perfetto_summary(report_dir)
        pf_state = _pf_state(discharge_pf)

        preferred_source = args.prefer
        discharge_pref = None
        p_avg_pref = None
        if args.prefer == "perfetto":
            # Only accept perfetto as preferred when it looks like a discharge run.
            if discharge_pf is not None and discharge_pf > 0:
                discharge_pref = discharge_pf
                p_avg_pref = p_avg_pf
                preferred_source = "perfetto"
            elif discharge_bs is not None:
                discharge_pref = discharge_bs
                p_avg_pref = _avg_power_W(discharge_bs, duration_s, v_mean_V)
                preferred_source = "batterystats"
            else:
                discharge_pref = discharge_cc
                p_avg_pref = _avg_power_W(discharge_cc, duration_s, v_mean_V)
                preferred_source = "charge_counter"
        elif args.prefer == "batterystats":
            discharge_pref = discharge_bs if discharge_bs is not None else discharge_cc
            preferred_source = "batterystats" if discharge_bs is not None else "charge_counter"
            p_avg_pref = _avg_power_W(discharge_pref, duration_s, v_mean_V)
        else:
            discharge_pref = discharge_cc if discharge_cc is not None else discharge_bs
            preferred_source = "charge_counter" if discharge_cc is not None else "batterystats"
            p_avg_pref = _avg_power_W(discharge_pref, duration_s, v_mean_V)

        invalid_reasons: list[str] = []
        if len(df) < args.min_rows:
            invalid_reasons.append(f"rows<{args.min_rows}")
        if duration_s is None:
            invalid_reasons.append("duration=NA")
        elif duration_s < args.min_duration_s:
            invalid_reasons.append(f"duration<{args.min_duration_s:.0f}s")

        is_valid = len(invalid_reasons) == 0
        invalid_reason = ";".join(invalid_reasons) if invalid_reasons else None

        # If preferred is perfetto and it provided power already, keep it; otherwise compute from discharge+V.
        p_avg = p_avg_pref

        points.append(
            Point(
                label=label,
                path=str(run_path).replace("\\", "/"),
                brightness=brightness,
                samples=int(len(df)),
                duration_s=duration_s,
                discharge_mAh_cc=discharge_cc,
                discharge_mAh_bs=discharge_bs,
                discharge_mAh_pf=discharge_pf,
                pf_state=pf_state,
                discharge_mAh_preferred=discharge_pref,
                preferred_source=preferred_source,
                v_mean_V=v_mean_V,
                p_avg_W_preferred=p_avg,
                energy_mWh_pf=energy_pf,
                is_valid=is_valid,
                invalid_reason=invalid_reason,
                bs_quantization_rel_err_max=bs_rel_err,
            )
        )

    out_df = pd.DataFrame([p.__dict__ for p in points])
    out_csv = args.out_dir / "s2_qc_points.csv"
    out_df.to_csv(out_csv, index=False, encoding="utf-8")

    # Plot: brightness vs discharge (preferred)
    plot_df = out_df.copy()
    plot_df = plot_df[plot_df["is_valid"] == True]  # noqa: E712
    plot_df = plot_df.dropna(subset=["brightness", "discharge_mAh_preferred"]).sort_values("brightness")

    fig, ax = plt.subplots(1, 1, figsize=(10, 5))
    if len(plot_df):
        x = plot_df["brightness"].astype(float)
        y = plot_df["discharge_mAh_preferred"].astype(float)

        ax.scatter(x, y)

        # Optional error bars if batterystats is the source (integer mAh -> +/-0.5 mAh)
        yerr = []
        for _, r in plot_df.iterrows():
            if str(r.get("preferred_source")) == "batterystats":
                yerr.append(0.5)
            else:
                yerr.append(0.0)
        if any(v > 0 for v in yerr):
            ax.errorbar(x, y, yerr=yerr, fmt="none", ecolor="tab:gray", alpha=0.5, capsize=3)

        for _, r in plot_df.iterrows():
            ax.annotate(str(r["label"]), (float(r["brightness"]), float(r["discharge_mAh_preferred"])), fontsize=8, alpha=0.8)

    ax.set_xlabel("Brightness")
    ax.set_ylabel("Discharge (mAh) over run")
    ax.set_title("S2: brightness vs discharge (valid runs only)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    out_png = args.out_dir / "s2_brightness_vs_discharge_mAh.png"
    fig.savefig(out_png, dpi=170)
    plt.close(fig)

    print(f"Wrote: {out_csv}")
    print(f"Wrote: {out_png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
