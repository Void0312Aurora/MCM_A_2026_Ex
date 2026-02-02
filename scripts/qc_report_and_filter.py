from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def _col_num(df: pd.DataFrame, col: str, default: float = np.nan) -> pd.Series:
    if col not in df.columns:
        return pd.Series([default] * len(df), index=df.index)
    return pd.to_numeric(df[col], errors="coerce")


def _read_first_row_csv(path: Path) -> dict:
    try:
        df = pd.read_csv(path, nrows=1)
    except Exception:
        return {}
    if df.empty:
        return {}
    r = df.iloc[0]
    out: dict = {}

    # Common columns in *_enriched.csv
    out["battery_level0_pct"] = float(pd.to_numeric(r.get("battery_level", np.nan), errors="coerce"))
    out["battery_voltage0_mV"] = float(pd.to_numeric(r.get("battery_voltage_mv", np.nan), errors="coerce"))

    # Thermal and charging
    out["thermal_cpu0_C"] = float(pd.to_numeric(r.get("thermal_cpu_C", np.nan), errors="coerce"))
    out["thermal_batt0_C"] = float(pd.to_numeric(r.get("thermal_battery_C", np.nan), errors="coerce"))
    out["thermal_status0"] = float(pd.to_numeric(r.get("thermal_status", np.nan), errors="coerce"))

    # Plugged flags can be blank in some logs; treat nonzero as plugged
    plugged = pd.to_numeric(r.get("battery_plugged", np.nan), errors="coerce")
    out["battery_plugged0"] = float(plugged) if pd.notna(plugged) else np.nan

    # Display
    out["display_state0"] = str(r.get("display_state", ""))
    out["brightness0"] = float(pd.to_numeric(r.get("brightness", np.nan), errors="coerce"))

    # Scenario
    out["scenario"] = str(r.get("scenario", ""))
    return out


def _read_perfetto_summary(report_dir: Path) -> dict:
    p = report_dir / "perfetto_android_power_summary.csv"
    if not p.exists():
        return {}
    try:
        df = pd.read_csv(p)
    except Exception:
        return {}
    if df.empty:
        return {}
    r = df.iloc[0]
    out = {
        "perfetto_power_mean_mW": float(pd.to_numeric(r.get("power_mw_mean", np.nan), errors="coerce")),
        "perfetto_energy_mWh": float(pd.to_numeric(r.get("energy_mwh", np.nan), errors="coerce")),
        "perfetto_current_mean_uA": float(pd.to_numeric(r.get("current_ua_mean", np.nan), errors="coerce")),
        "perfetto_voltage_mean_V": float(pd.to_numeric(r.get("voltage_v_mean", np.nan), errors="coerce")),
        "perfetto_discharge_mAh": float(pd.to_numeric(r.get("discharge_mah", np.nan), errors="coerce")),
        "perfetto_duration_s": float(pd.to_numeric(r.get("duration_s", np.nan), errors="coerce")),
    }
    return out


def build_run_qc_table(runs_dir: Path, reports_dir: Path) -> pd.DataFrame:
    rows: list[dict] = []

    for run_csv in sorted(runs_dir.glob("*_enriched.csv")):
        run_name = run_csv.stem.replace("_enriched", "")
        info = _read_first_row_csv(run_csv)
        info["run_name"] = run_name
        info["run_csv"] = str(run_csv.as_posix())

        # Find matching report dir by prefix run_id
        run_id = run_name.split("_")[:2]
        run_id = "_".join(run_id) if len(run_id) == 2 else run_name
        report_matches = sorted(reports_dir.glob(f"{run_id}_*"))
        report_dir = None
        for m in report_matches:
            if m.is_dir() and (m / "perfetto_android_power_timeseries.csv").exists():
                report_dir = m
                break

        info["report_dir"] = str(report_dir.as_posix()) if report_dir else ""
        info["has_perfetto"] = 1 if report_dir else 0
        if report_dir:
            info.update(_read_perfetto_summary(report_dir))
        rows.append(info)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Basic normalization
    for c in [
        "battery_level0_pct",
        "battery_voltage0_mV",
        "thermal_cpu0_C",
        "thermal_batt0_C",
        "thermal_status0",
        "battery_plugged0",
        "brightness0",
        "perfetto_power_mean_mW",
        "perfetto_current_mean_uA",
        "perfetto_voltage_mean_V",
        "perfetto_discharge_mAh",
        "perfetto_duration_s",
    ]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    return df


def apply_qc_policy(
    run_qc: pd.DataFrame,
    *,
    min_soc_pct: float,
    min_voltage_mV: float,
    max_thermal_cpu_C: float,
    require_thermal_status0: bool,
    require_unplugged: bool,
    require_perfetto: bool,
) -> pd.DataFrame:
    df = run_qc.copy()

    reasons = []
    keep = np.ones(len(df), dtype=bool)

    def add_reason(mask: np.ndarray, reason: str) -> None:
        nonlocal reasons, keep
        reasons.append((mask, reason))
        keep &= ~mask

    if require_perfetto:
        m = df.get("has_perfetto", 0).astype(int).to_numpy() == 0
        add_reason(m, "no_perfetto")

    soc = df.get("battery_level0_pct", pd.Series([np.nan] * len(df))).to_numpy(float)
    m = np.isfinite(soc) & (soc < float(min_soc_pct))
    add_reason(m, f"soc<{min_soc_pct}")

    v = df.get("battery_voltage0_mV", pd.Series([np.nan] * len(df))).to_numpy(float)
    m = np.isfinite(v) & (v < float(min_voltage_mV))
    add_reason(m, f"voltage<{min_voltage_mV}mV")

    t = df.get("thermal_cpu0_C", pd.Series([np.nan] * len(df))).to_numpy(float)
    m = np.isfinite(t) & (t > float(max_thermal_cpu_C))
    add_reason(m, f"thermal_cpu0>{max_thermal_cpu_C}C")

    if require_thermal_status0:
        ts = df.get("thermal_status0", pd.Series([np.nan] * len(df))).to_numpy(float)
        m = np.isfinite(ts) & (ts != 0)
        add_reason(m, "thermal_status!=0")

    if require_unplugged:
        bp = df.get("battery_plugged0", pd.Series([np.nan] * len(df))).to_numpy(float)
        m = np.isfinite(bp) & (bp != 0)
        add_reason(m, "plugged")

    # Build reason strings
    reason_str = np.array([""] * len(df), dtype=object)
    for mask, r in reasons:
        for i in np.where(mask)[0]:
            if reason_str[i]:
                reason_str[i] += ";"
            reason_str[i] += r

    df["qc_keep"] = keep.astype(int)
    df["qc_reject_reasons"] = reason_str
    return df


def scenario_repeatability(run_qc: pd.DataFrame) -> pd.DataFrame:
    df = run_qc.copy()
    if df.empty:
        return df

    # Use perfetto mean power when present
    p = df.get("perfetto_power_mean_mW")
    if p is None:
        return pd.DataFrame()

    out_rows = []
    for scen, g in df.groupby("scenario"):
        vals = pd.to_numeric(g["perfetto_power_mean_mW"], errors="coerce").dropna().to_numpy(float)
        if len(vals) < 2:
            continue
        mean = float(np.mean(vals))
        std = float(np.std(vals, ddof=1))
        cv = float(std / mean) if mean != 0 else np.nan
        ratio = float(np.max(vals) / np.min(vals)) if np.min(vals) > 0 else np.nan
        out_rows.append(
            {
                "scenario": str(scen),
                "n": int(len(vals)),
                "p_mean_mW_mean": mean,
                "p_mean_mW_std": std,
                "p_mean_mW_cv": cv,
                "p_mean_mW_ratio_max_min": ratio,
                "p_mean_mW_min": float(np.min(vals)),
                "p_mean_mW_max": float(np.max(vals)),
            }
        )

    if not out_rows:
        return pd.DataFrame()
    return pd.DataFrame(out_rows).sort_values(["p_mean_mW_ratio_max_min", "p_mean_mW_cv"], ascending=False)


def filter_model_input(model_input_csv: Path, run_qc: pd.DataFrame, out_csv: Path) -> None:
    df = pd.read_csv(model_input_csv)
    keep_runs = set(run_qc.loc[run_qc["qc_keep"].astype(int) == 1, "run_name"].astype(str))
    df2 = df[df["run_name"].astype(str).isin(keep_runs)].copy()
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df2.to_csv(out_csv, index=False, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Generate a QC + repeatability report from existing artifacts, and optionally emit a filtered model_input.csv. "
            "This is designed for last-day triage (no re-tests required)."
        )
    )
    ap.add_argument("--runs-dir", type=Path, default=Path("artifacts/runs"))
    ap.add_argument("--reports-dir", type=Path, default=Path("artifacts/reports"))
    ap.add_argument("--model-input", type=Path, default=Path("artifacts/models/all_runs_model_input.csv"))
    ap.add_argument("--out-dir", type=Path, default=Path("artifacts/qc"))

    ap.add_argument("--min-soc-pct", type=float, default=50.0)
    ap.add_argument("--min-voltage-mV", type=float, default=3700.0)
    ap.add_argument("--max-thermal-cpu-C", type=float, default=60.0)
    ap.add_argument("--require-thermal-status0", action="store_true")
    ap.add_argument("--require-unplugged", action="store_true")
    ap.add_argument("--require-perfetto", action="store_true")

    ap.add_argument("--emit-filtered-model-input", action="store_true")

    args = ap.parse_args()

    run_qc = build_run_qc_table(args.runs_dir, args.reports_dir)
    if run_qc.empty:
        print("No runs found.")
        return 1

    run_qc = apply_qc_policy(
        run_qc,
        min_soc_pct=float(args.min_soc_pct),
        min_voltage_mV=float(args.min_voltage_mV),
        max_thermal_cpu_C=float(args.max_thermal_cpu_C),
        require_thermal_status0=bool(args.require_thermal_status0),
        require_unplugged=bool(args.require_unplugged),
        require_perfetto=bool(args.require_perfetto),
    )

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    out_runs = out_dir / "qc_run_summary.csv"
    run_qc.sort_values(["qc_keep", "has_perfetto", "scenario", "run_name"], ascending=[False, False, True, True]).to_csv(
        out_runs, index=False, encoding="utf-8"
    )

    rep = scenario_repeatability(run_qc)
    out_rep = out_dir / "qc_scenario_repeatability.csv"
    rep.to_csv(out_rep, index=False, encoding="utf-8")

    kept = int(run_qc["qc_keep"].astype(int).sum())
    total = int(len(run_qc))
    print(f"QC kept {kept}/{total} runs")
    print(f"Wrote: {out_runs}")
    print(f"Wrote: {out_rep}")

    if args.emit_filtered_model_input:
        out_model = Path("artifacts/models/all_runs_model_input_qc.csv")
        filter_model_input(args.model_input, run_qc, out_model)
        print(f"Wrote: {out_model}")

    # Quick console: top 10 most non-repeatable scenarios (if any)
    if not rep.empty:
        print("\nTop non-repeatable scenarios (by max/min ratio):")
        print(rep.head(10).to_string(index=False, float_format=lambda x: f"{x:8.3f}"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
